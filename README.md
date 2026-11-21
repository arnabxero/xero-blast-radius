# Team-Xero: Blast Radius selector

Entry for the RWX **Blast Radius** track of the Builders Buildathon. For each patch to sqlglot (pinned at `eb11aec`), it prints the tests that fail and how to split them across workers.

## How it decides

1. **Apply the patch in memory.** `selector/patchutil.py` parses the unified diff and applies it against the pristine sqlglot checkout baked into the image. It handles new, deleted and renamed files.
2. **Find candidate tests from maps built at the pinned commit** (`maps/coverage.json.gz`, built by `lab/`):
   - Each test was run in its own process with a `sys.monitoring` line tracer (`lab/trace_plugin.py`). This records the sqlglot lines it executes, the files it opens, and the sqlglot classes it instantiates (with their MRO).
   - Process pools used by the optimizer and executor tests run in-process during tracing, with the same pickle round trip, so their work is traced too.
   - **Change inside a function or lambda body** → tests that executed that function.
   - **Change to a class body** (class attributes such as `arg_types`, `KEYWORDS`, `WEEK_OFFSET`) → tests that instantiated that class or a subclass.
   - **Module-level change, class header change, or global-registry attribute** (`_sql_names`, `key`) → every test that ran or imported the module.
   - **Non-Python file** (SQL fixtures and others) → tests that opened it.
   - **Comment- or whitespace-only edits** to Python files → no tests (compared token by token, ignoring comments).
3. **Run the cheap candidates on the patched code.** A forked child serves the patched modules through an import hook and patched data files through `open()`, then runs the candidate tests with `unittest`.
   - Errors, failures, failing sub-tests, timeouts and import errors all count as failures, as they do in pytest.
   - Spawn-based process pools run inline, with the same pickle round trip.
   - Tests slower than 1 s are listed without running, since running one costs about as much as listing it.
4. **Print only failing tests**, split across k = 1..8 workers to minimize `max load + 2 s × k`.

Anything unexpected (a patch that doesn't apply, a crash in the child, a binary patch) falls back to listing every candidate, never to an empty answer.

## Layout

- `selector/`: the program that runs in the scorer (`main.py`, `patchutil.py`, `runner.py`).
- `maps/coverage.json.gz`: coverage, fixture and class maps built from the pinned commit.
- `lab/`: tools that build the maps (`trace_plugin.py`, `trace_all.sh`, `build_maps.py`), the pytest labeler used for local checks, and the sample evaluator.
- `tools/fetch_sqlglot.py`: build step that downloads sqlglot at the pinned commit from GitHub.

## Third-party code

- sqlglot (MIT), downloaded at build time.
- Python packages from the organizers' `requirements.lock`, installed from PyPI.

## License

MIT, see `LICENSE`.
