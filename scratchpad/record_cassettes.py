"""Record the VCR cassettes for the recordable test set against the verified sandbox.

Uses the datadirtest scaffolder per-test so we control, per case, (a) the real TBA creds injected
for recording (deep-merged in memory; on-disk config.json keeps the dummy values) and (b) the seeded
input state for incremental cases. The 401 / token-expiry cases get a deliberately corrupted token
secret so NetSuite returns a real 401. Secret values are never printed.

    uv run python scratchpad/record_cassettes.py [test_name_substring ...]
"""

import json
import os
import shutil
import sys
from pathlib import Path
from runpy import run_path

from keboola.datadirtest.vcr.tester import _load_vcr_sanitizers_from_script
from keboola.vcr import save_output_snapshot
from keboola.vcr.recorder import VCRRecorder
from keboola.vcr.scaffolder import TestScaffolder

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_SECRETS = ROOT / "secrets.env"
_DEFINITIONS = ROOT / "tests" / "setup" / "configs.json"
_OUTPUT = ROOT / "tests" / "functional"
_COMPONENT = ROOT / "src" / "component.py"
# No freeze-time: NetSuite TBA rejects OAuth/TokenPassport timestamps outside a tight tolerance
# window, so a frozen (stale) clock makes every signed request 401. Replay does not need a frozen
# clock here — the only time-dependent value (the incremental watermark) comes from NetSuite's own
# response, and the signed Authorization header is stripped and never matched.
_FREEZE = None

# Cases that must record a real auth failure -> corrupt the token secret.
_CORRUPT_AUTH = {"02_testConnection_401", "20_suiteql_token_expiry"}
# The datadirtest replay path resets in/state.json to {} (TestDataDir._override_input_state) unless a
# per-test last_state_override is passed, so a seeded watermark can't survive replay. The incrementals
# therefore run as a first incremental run (no prior state): suiteql still exercises the :state binding
# (defaults to the 1970 epoch) and both write a fresh watermark; the ON_OR_AFTER / :state injection
# from an existing watermark is covered by the extractor unit tests.
_SEEDED_STATE: dict[str, dict] = {}


def _load_env() -> dict[str, str]:
    v: dict[str, str] = {}
    for line in _SECRETS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, val = line.partition("=")
            v[k.strip()] = val.strip().strip('"').strip("'")
    return v


def _real_params(env: dict[str, str], corrupt: bool) -> dict:
    token_secret = env["NETSUITE_TOKEN_SECRET"] + ("_corrupt" if corrupt else "")
    return {
        "parameters": {
            "account_id": env["NETSUITE_ACCOUNT_ID"],
            "#consumer_key": env["NETSUITE_CONSUMER_KEY"],
            "#consumer_secret": env["NETSUITE_CONSUMER_SECRET"],
            "#token_id": env["NETSUITE_TOKEN_ID"],
            "#token_secret": token_secret,
        }
    }


def _run_component(sdd: Path) -> None:
    os.environ["KBC_DATADIR"] = str(sdd)
    from keboola.component.base import ComponentBase

    orig = ComponentBase._should_vcr_replay
    ComponentBase._should_vcr_replay = staticmethod(lambda: False)  # ty: ignore[invalid-assignment]
    try:
        run_path(str(_COMPONENT), run_name="__main__")
    finally:
        ComponentBase._should_vcr_replay = orig


def _resnapshot(name: str) -> None:
    """Regenerate expected/ + output_snapshot from a REPLAY of the sanitized cassette.

    The scaffolder captures expected output from the LIVE (unsanitized) response, so any response
    sanitization (e.g. email PII redaction) leaves expected/ holding the raw value while replay
    produces the redacted value -> mismatch, and raw PII committed. Replaying against the sanitized
    cassette and re-snapshotting makes expected/ reflect exactly what replay produces.
    """
    test_dir = _OUTPUT / name
    sdd = test_dir / "source" / "data"
    exp_out = test_dir / "expected" / "data" / "out"
    for base in (sdd / "out", exp_out):
        for sub in ("tables", "files"):
            p = base / sub
            if p.exists():
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)
    sanitizers = _load_vcr_sanitizers_from_script(str(_COMPONENT))
    rec = VCRRecorder.from_test_dir(sdd, sanitizers=sanitizers or None)
    try:
        rec.replay(lambda: _run_component(sdd))
    except SystemExit:
        pass  # failure tests exit(1); they produce no output tables
    for sub in ("tables", "files"):
        s = sdd / "out" / sub
        if s.exists():
            for item in s.iterdir():
                if item.is_file():
                    shutil.copy2(item, exp_out / sub / item.name)
    save_output_snapshot(sdd, output_subdir="out")


def main() -> None:
    env = _load_env()
    definitions = json.loads(_DEFINITIONS.read_text())
    wanted = sys.argv[1:]
    scaffolder = TestScaffolder()

    for definition in definitions:
        name = definition["name"]
        if wanted and not any(w in name for w in wanted):
            continue
        secrets_override = _real_params(env, corrupt=name in _CORRUPT_AUTH)
        input_state = _SEEDED_STATE.get(name, {})
        print(f"recording {name} ...")
        try:
            scaffolder._scaffold_single_test(  # noqa: SLF001
                definition=definition,
                output_dir=_OUTPUT,
                component_script=_COMPONENT,
                record=True,
                freeze_time_at=_FREEZE,
                secrets_override=secrets_override,
                input_state=input_state,
                regenerate=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {name} recording raised: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        try:
            _resnapshot(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {name} resnapshot raised: {type(exc).__name__}: {str(exc)[:200]}")

    print("done.")


if __name__ == "__main__":
    main()
