"""axe-core audit helper shared by the e2e tests (axe ships with axe-playwright-python)."""

from axe_playwright_python.sync_playwright import Axe

BLOCKING = {"serious", "critical"}
OPTIONS = {"resultTypes": ["violations"]}


def audit(page, name):
    """Run axe on the loaded page; fail on serious / critical violations."""
    results = Axe().run(page, options=OPTIONS)
    blocking = [v for v in results.response["violations"] if v["impact"] in BLOCKING]
    detail = "\n".join(
        f"{v['impact']} {v['id']}: {v['help']} ({v['helpUrl']})\n"
        + "\n".join(
            f"    {n['target']} {n.get('failureSummary', '')[:300]}" for n in v["nodes"][:3]
        )
        for v in blocking
    )
    assert not blocking, f"axe found blocking violations on {name}:\n{detail}"
