"""RNA-seq vendor payload ingestion pipeline.

Layering (see docs/DECISIONS.md → D5):

    parse → validate → normalise → load

The first three stages are pure functions with no database dependency, so they are
unit-testable without Postgres and survive the platform migration described in the platform-design notes.
Only ``load`` touches the database, inside a single transaction per payload.
"""

__version__ = "0.1.0"
