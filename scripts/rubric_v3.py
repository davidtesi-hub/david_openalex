"""Version 3: auditable abstract-only scientific importance rubric."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

VERSION = 3
WEIGHTS = (20, 40, 20, 20)

# This is the user's instruction, with only its explicitly requested line-count correction.
INSTRUCTION = '''You evaluate the potential scientific importance of a paper from ONLY
the text inside <abstract>...</abstract>.

Judge the main contribution that the abstract reports as ALREADY
ACHIEVED. Do not reward planned results. Do not use external knowledge,
the authors' reputation, the venue, expected citations, or writing
style. Do not treat "first", "novel", "fundamental", or "transformative"
as evidence unless the abstract states the concrete result or
difference behind the word.

A theorem, derivation, observation, measurement, constraint,
informative null result, simulation, method, dataset, or evidence
synthesis may be important. Judge what it establishes or enables,
not the kind of paper.

First classify the main contribution. This label has NO effect on
the score:
T = T: theoretical or mathematical result
    E: empirical or observational result
    M: method, instrument, simulation tool, or data resource
    S: synthesis that states a scientific conclusion
    P: proposal, course, overview, roadmap, or other text without
       a specific new scientific result
    U: type cannot be determined

Then answer Q1–Q4. Choose exactly one option A, B, C, D, or U for
each question. A–D are ordered from lower to higher. U means the
abstract lacks the information needed for that question. U is never
a middle score.

Q1. How important is the SPECIFIC scientific question addressed,
regardless of how much this paper advances it?
A: A narrowly technical question with limited scientific consequences.
B: A useful question within one specialty.
C: An important question for a substantial research area.
D: A central question for a field or multiple fields.
U: The question addressed cannot be identified.
A fundamental-sounding topic alone does not justify D.

Q2. How much progress does the abstract report as ALREADY ACHIEVED
on that question?
A: No specific new scientific result is reported.
B: A specific but limited result, improvement, confirmation,
   synthesis, or research resource is reported.
C: The result substantially reduces a meaningful uncertainty,
   resolves an important part of the question, or demonstrably
   enables a significant class of investigations.
D: Within clearly stated conditions, the result resolves a central
   question, decisively distinguishes important alternatives, or
   establishes a broadly consequential new capability.
U: The abstract does not let you identify the achieved contribution
   or judge its extent.
A theoretical or mathematical result need not be experimentally
confirmed. An informative null result may receive C or D. A
proposal receives no credit for results expected in the future.

Q3. How broadly does the SPECIFIC RESULT apply, according to the
abstract?
A: One particular system, sample, model, or configuration.
B: A defined family of systems or problems within a specialty.
C: Several important problems or research directions within a field.
D: A broad class of problems or concretely stated implications
   across fields.
U: The result's scope cannot be determined.
A large sample or catalogue does not by itself establish generality.
Do not invent applications beyond those linked to the result.

Q4. What concrete difference from previous work does the abstract
DESCRIBE? Judge the stated difference, not whether priority is true.
A: It explicitly describes applying or reproducing an established
   approach without a substantive new knowledge contribution.
B: It describes a specific improvement in precision, coverage,
   formulation, confirmation, or synthesis.
C: It describes a distinct result, test, explanation, proof, method,
   or capability relative to an earlier limitation or alternative.
D: It describes a concrete change to a central assumption or a
   previously unresolved answer, or opens a new class of investigation.
U: It gives no usable comparison with previous work.
An unexplained claim of being "first" or "novel" cannot justify C
or D. Lack of a comparison is U, not A.

For every Q answer, copy one SHORT, EXACT excerpt from the abstract
that supports it. For U, write "-" instead of an excerpt.

For C or D, also write ONE short sentence naming the concrete feature
that warrants the chosen level over the level immediately below it.
For A, B, or U, write "-" instead of a sentence.

Choose the highest level supported by concrete text. If the text
does not establish the feature that separates two adjacent levels,
choose the lower level. Do not infer missing facts from field knowledge.
The excerpt and sentence explain your answer for later audit; they
are not additional scoring criteria.

Return exactly these thirteen lines, in this order. Do not output a
numerical score or any other text:

T=<T|E|M|S|P|U>
Q1=<A|B|C|D|U>
E1="<exact excerpt or ->"
J1="<one sentence if Q1 is C or D; otherwise ->"
Q2=<A|B|C|D|U>
E2="<exact excerpt or ->"
J2="<one sentence if Q2 is C or D; otherwise ->"
Q3=<A|B|C|D|U>
E3="<exact excerpt or ->"
J3="<one sentence if Q3 is C or D; otherwise ->"
Q4=<A|B|C|D|U>
E4="<exact excerpt or ->"
J4="<one sentence if Q4 is C or D; otherwise ->"'''

KEYS = ['T'] + [key for i in range(1, 5) for key in (f'Q{i}', f'E{i}', f'J{i}')]
LINE = re.compile(r'^([TEJQ][1-4]?)=(.*)$')


def prompt(abstract: str) -> str:
    return INSTRUCTION + '\n\n<abstract>\n' + abstract.strip() + '\n</abstract>'


def parse_response(raw: str, abstract: str) -> dict:
    lines = raw.strip().splitlines()
    if len(lines) != 13:
        raise ValueError(f'Expected thirteen lines, got {len(lines)}')
    parsed = {}
    for expected, line in zip(KEYS, lines):
        match = LINE.fullmatch(line.strip())
        if match is None or match.group(1) != expected:
            raise ValueError(f'Expected {expected} line')
        value = match.group(2)
        if expected.startswith(('E', 'J')):
            if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
                raise ValueError(f'{expected}: quoted value required')
            value = value[1:-1]
        parsed[expected] = value
    if parsed['T'] not in 'TEMSPU' or len(parsed['T']) != 1:
        raise ValueError('Invalid T')
    known = missing = 0
    for i, weight in enumerate(WEIGHTS, 1):
        q, e, j = (parsed[f'{prefix}{i}'] for prefix in 'QEJ')
        if q not in 'ABCDU' or len(q) != 1:
            raise ValueError(f'Invalid Q{i}')
        if q == 'U':
            missing += weight
            if e != '-':
                raise ValueError(f'E{i} must be - for U')
        else:
            known += weight * 'ABCD'.index(q)
            if not e or e == '-' or e not in abstract:
                raise ValueError(f'E{i} is not an exact abstract excerpt')
        if q in 'CD':
            if not j or j == '-':
                raise ValueError(f'J{i} required for C or D')
        elif j != '-':
            raise ValueError(f'J{i} must be - for A, B or U')
    parsed['coverage_percent'] = 100 - missing
    parsed['minimum_score'] = quantize(Decimal(known) / 30)
    parsed['maximum_score'] = quantize(Decimal(known + 3 * missing) / 30)
    parsed['score'] = None if missing else parsed['minimum_score']
    return parsed


def quantize(value: Decimal) -> str:
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('''CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY, work_id TEXT NOT NULL, modelo TEXT NOT NULL,
        version_rubrica INTEGER NOT NULL CHECK(version_rubrica=3),
        puntuacion TEXT, T TEXT, Q1 TEXT, E1 TEXT, J1 TEXT,
        Q2 TEXT, E2 TEXT, J2 TEXT, Q3 TEXT, E3 TEXT, J3 TEXT,
        Q4 TEXT, E4 TEXT, J4 TEXT,
        coverage_percent INTEGER, minimum_score TEXT, maximum_score TEXT,
        abstract_sha256 TEXT NOT NULL, response_raw TEXT NOT NULL,
        validation_error TEXT, created_at_utc TEXT NOT NULL,
        latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER)''')
    db.execute('CREATE INDEX IF NOT EXISTS evaluations_v3_lookup ON evaluations(work_id, validation_error)')
    db.commit()
    return db


def save_response(db: sqlite3.Connection, *, work_id: str, model: str, abstract: str,
                  raw: str, latency_ms=None, input_tokens=None, output_tokens=None) -> int:
    values = dict(work_id=work_id, modelo=model, version_rubrica=VERSION,
                  abstract_sha256=hashlib.sha256(abstract.encode()).hexdigest(),
                  response_raw=raw, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens)
    try:
        result = parse_response(raw, abstract)
        values.update({key: result[key] for key in KEYS})
        values.update(puntuacion=result['score'], coverage_percent=result['coverage_percent'],
                      minimum_score=result['minimum_score'], maximum_score=result['maximum_score'])
    except ValueError as exc:
        values['validation_error'] = str(exc)
    keys = list(values)
    cursor = db.execute(f"INSERT INTO evaluations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(values.values()))
    db.commit()
    return cursor.lastrowid
