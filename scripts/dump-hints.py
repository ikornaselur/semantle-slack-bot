import heapq
import json
import multiprocessing as mp
import re
import sqlite3
from collections import namedtuple
from functools import partial
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

import gensim.models.keyedvectors as word2vec
from numpy import dot
from numpy.linalg import norm
from rich.console import Console
from rich.progress import MofNCompleteColumn, Progress, TimeElapsedColumn

ENGLISH_WORDS = Path(__file__).parent / "wordlists/english.txt"
BAD_WORDS = Path(__file__).parent / "wordlists/bad.txt"
TARGET_WORDS = Path(__file__).parent.parent / "src/semantle_slack_bot/target_words.json"
VECTORS = str(Path(__file__).parent.parent / "GoogleNews-vectors-negative300.bin")

Word = namedtuple("Word", ["name", "vec", "norm"])
Similarities = list[tuple[float, str]]

PROCESSES = max(mp.cpu_count() // 2, 1)
CHUNK_SIZE = 100
DB_NAME = "word2vec.db"

console = Console()


def chunked(iterable: Iterable, n: int = 1000) -> Iterator:
    def take(n: int, iterable: Iterable) -> list:
        return list(islice(iterable, n))

    return iter(partial(take, n, iter(iterable)), [])


def make_words() -> dict[str, Word]:
    console.log("Load vectors into model")

    with console.status("Importing..."):
        model: word2vec.KeyedVectors = word2vec.KeyedVectors.load_word2vec_format(
            VECTORS, binary=True
        )

    console.log("Loading english wordlist")
    with open(ENGLISH_WORDS, "r") as english_words_file:
        english_words = {line.strip() for line in english_words_file.readlines()}

    console.log("Loading bad word list")
    with open(BAD_WORDS, "r") as bad_words_file:
        bad_words = {line.strip() for line in bad_words_file.readlines()}

    wordlist = english_words - bad_words

    simple_word = re.compile("^[a-z]*$")
    words = {}
    for word in model.key_to_index:
        if simple_word.match(word) and word in wordlist:
            vec = model[word]
            words[word] = Word(name=word, vec=vec, norm=norm(vec))

    return words


def find_hints(
    words: list[Word],
    target: Word,
) -> tuple[str, Similarities]:
    """Return hints for the 1,000 closest words"""
    target_vec = target.vec
    target_vec_norm = target.norm

    similarities: Similarities = []

    for word in words:
        similarity = float(dot(word.vec, target_vec) / (word.norm * target_vec_norm))
        if len(similarities) < 1000:
            heapq.heappush(similarities, (similarity, word.name))
        elif similarity > similarities[0][0]:
            heapq.heappushpop(similarities, (similarity, word.name))

    return (target.name, list(sorted(similarities)))


def store_hints(nearest: dict[str, Similarities]) -> None:
    console.log("Creating tables")
    con = sqlite3.connect(DB_NAME)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    cur.execute(
        """create table if not exists nearby
        (word text, neighbor text, similarity float, percentile integer,
        PRIMARY KEY (word, neighbor))"""
    )
    cur.execute(
        """create table if not exists similarity_range
        (word text PRIMARY KEY, top float, top10 float, rest float)"""
    )
    con.commit()

    with Progress(
        *Progress.get_default_columns(),
        TimeElapsedColumn(),
        MofNCompleteColumn(),
    ) as progress:
        with con:
            con.execute("DELETE FROM nearby")
            con.execute("DELETE FROM similarity_range")
            for secret, neighbors in progress.track(
                nearest.items(), description="Inserting hints to tables..."
            ):
                con.executemany(
                    (
                        "insert into nearby (word, neighbor, similarity, percentile) "
                        "values (?, ?, ?, ?)"
                    ),
                    (
                        (secret, neighbor, "%s" % score, (1 + idx))
                        for idx, (score, neighbor) in enumerate(neighbors)
                    ),
                )

                top = neighbors[-2][0]
                top10 = neighbors[-11][0]
                rest = neighbors[0][0]
                con.execute(
                    (
                        "insert into similarity_range (word, top, top10, rest) "
                        "values (?, ?, ?, ?)"
                    ),
                    (secret, "%s" % top, "%s" % top10, "%s" % rest),
                )

    con.commit()


def main() -> None:
    words = make_words()

    console.log("Load target words")
    with open(TARGET_WORDS, "r") as f:
        target_words = json.load(f)

    console.log(f"Initialising multiprocessing pool with {PROCESSES} processes")
    pool = mp.Pool(processes=PROCESSES)
    words_values = list(words.values())

    hints: dict[str, Similarities] = {}

    with Progress(
        *Progress.get_default_columns(),
        TimeElapsedColumn(),
        MofNCompleteColumn(),
    ) as progress:
        task = progress.add_task(
            description="Finding hints for words...",
            total=len(target_words),
        )
        for word, nearest in pool.imap_unordered(
            partial(find_hints, words_values),
            (words[t] for t in target_words),
            chunksize=CHUNK_SIZE,
        ):
            hints[word] = nearest
            progress.advance(task, 1)

    store_hints(hints)


if __name__ == "__main__":
    main()
