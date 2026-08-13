"""Ask questions of a folder of documents.

The whole file. Everything else is a platform default:

    navigator-orchestrator check examples/qa.py
    navigator-orchestrator run   examples/qa.py --question "what is the refund window?" --dir ./docs

Delete every function below and it still runs — see `examples/minimal.py`.
"""

WORKFLOW = "doc-qa"


def index(ctx, sources, question):
    """Put documents mentioning the question's words first, and cap the corpus.

    Not required. It exists to show the shape: a hook returns data, the engine
    does the rest, and there is no loop or branch to write.
    """
    words = {word.strip("?.,").lower() for word in question.split() if len(word) > 3}
    ranked = sorted(sources, key=lambda d: -len(words & set(d.text.lower().split())))
    ctx.note(f"ranked {len(ranked)} document(s); keeping up to 12")
    return ranked[:12]
