"""Input normalisation for the sequence branch.

`normalize_sequence` turns whatever the user pasted (raw nucleotides, RNA with U,
lower case, a single FASTA record, Windows line endings) into the canonical form the
predictors expect: upper-case, A/C/G/T only, within the configured length limits.

It is a pure function so the length limits can come from `Settings` at call time,
and so it can be unit-tested without FastAPI. Every rejection raises
`SequenceValidationError`, which `app.main` maps to HTTP 422.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.predictors.base import MIN_SEQUENCE_NT

ALLOWED_NUCLEOTIDES = frozenset("ACGUT")
DEFAULT_MAX_SEQUENCE_NT = 10_000
_MAX_REPORTED_OFFENDERS = 5


class SequenceValidationError(ValueError):
    """The submitted sequence cannot be scored. The message is safe to show to the user."""


@dataclass(frozen=True)
class NormalizedSequence:
    sequence: str  # upper-case, only A/C/G/T
    transcript_id: str | None  # first token of the FASTA header, if there was one
    had_u: bool  # input contained U (RNA alphabet); it has been mapped to T
    had_fasta_header: bool


def normalize_sequence(
    raw: str,
    *,
    min_nt: int = MIN_SEQUENCE_NT,
    max_nt: int = DEFAULT_MAX_SEQUENCE_NT,
) -> NormalizedSequence:
    """Validate and canonicalise a user-supplied sequence.

    Rules, applied in this order:
      a. A leading ``>`` line is a FASTA header; its first token becomes ``transcript_id``.
         A second ``>`` line means several records were pasted -> rejected.
      b. All whitespace (spaces, tabs, CR, LF) is removed.
      c. The sequence is upper-cased.
      d. Any character outside A/C/G/U/T is rejected (up to 5 distinct offenders listed).
      e. U is mapped to T.
      f. Length must be within [min_nt, max_nt].
    """
    # Cheap guard before any per-character work: a pasted FASTA line-wrapped at 60 columns
    # plus a header is < 1.1x the sequence length, so anything over 2x + slack cannot be a
    # valid input. Bounds the CPU spent on abusive multi-megabyte bodies.
    if len(raw) > 2 * max_nt + 1024:
        raise SequenceValidationError(
            f"input too long: {len(raw)} characters (sequence must be at most {max_nt} nt)"
        )
    text = raw.lstrip("\ufeff \t\r\n")  # tolerate a BOM / blank lines before the header
    transcript_id: str | None = None
    had_fasta_header = False

    if text.startswith(">"):
        had_fasta_header = True
        header, _, text = text.partition("\n")
        tokens = header[1:].split()
        transcript_id = tokens[0] if tokens else None

    if any(line.lstrip().startswith(">") for line in text.splitlines()):
        raise SequenceValidationError("only one sequence per request is supported")

    sequence = "".join(text.split()).upper()

    if set(sequence) - ALLOWED_NUCLEOTIDES:
        offenders: list[str] = []
        for ch in sequence:
            if ch not in ALLOWED_NUCLEOTIDES and ch not in offenders:
                offenders.append(ch)
                if len(offenders) == _MAX_REPORTED_OFFENDERS:
                    break
        listed = ", ".join(repr(ch) for ch in offenders)
        raise SequenceValidationError(
            f"invalid character(s) in sequence: {listed} (allowed: A, C, G, U/T)"
        )

    had_u = "U" in sequence
    if had_u:
        sequence = sequence.replace("U", "T")

    n = len(sequence)
    if n < min_nt:
        raise SequenceValidationError(
            f"sequence too short: {n} nt after removing whitespace (must be at least {min_nt} nt)"
        )
    if n > max_nt:
        raise SequenceValidationError(f"sequence too long: {n} nt (must be at most {max_nt} nt)")

    return NormalizedSequence(
        sequence=sequence,
        transcript_id=transcript_id,
        had_u=had_u,
        had_fasta_header=had_fasta_header,
    )
