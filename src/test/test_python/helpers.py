"""Shared test helpers.

Formats live in the database, so a test that imports a CSV has to teach the database
that CSV's layout first — the same inference the interactive setup uses.
"""

from budget_tracker import formats
from budget_tracker.importer import read_header_and_rows


def learn_format(session, path, name="test_layout"):
    """Infer and save the format for ``path``, as interactive setup would.

    Genuinely ambiguous fixtures (an unrecognizable column name, say) still need a real
    answer and are not handled here. But a question with a default or a fixed set of
    choices — the ambiguous-date and invert-amount questions among them — is resolved
    the way accepting the suggestion in the walkthrough would, so fixtures written
    before those questions existed keep working unchanged.
    """
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer(name, fieldnames, rows)
    values, questions = inference.values, inference.questions
    while questions:
        answers = {}
        for question in questions:
            if question.default is not None:
                answers[question.field] = question.default
            elif question.choices:
                answers[question.field] = question.choices[0]
            else:
                raise AssertionError(f"learn_format cannot answer {question.field!r}")
        values = formats.apply_answers(values, answers, fieldnames, rows)
        questions = formats.remaining_questions(values, rows, fieldnames)
    spec = formats.spec_from_values(values)
    formats.save_format(session, spec)
    session.commit()
    return spec
