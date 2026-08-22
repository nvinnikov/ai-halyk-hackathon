# Contributing

This is a competition solution archive for the Halyk AI Challenge (August 2026).
Bug reports and questions are welcome; major architectural changes are unlikely
to be merged.

## Reporting issues

If you find a reproducible discrepancy with the private-set proxy key, please
include:

1. The borrower ID and covenant cell
2. Expected vs actual output
3. The trace file from `work/<hash>/trace/`

## Running tests

```bash
make check          # lint + typecheck + unit tests
make eval-offline   # invariant checks, grep-gate, cassette replay (no network)
make solve          # full pipeline on public set
make private-score  # against proxy key (needs private dataset)
```

## Code of conduct

Be constructive. Disagreement is fine; bad faith is not.
