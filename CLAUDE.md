# CLAUDE.md

## Tests

pytest is not installed. Run the suite with unittest from the `tests` folder:

```
cd tests && PYTHONPATH=.. ../.venv/bin/python -m unittest discover -s .
```

Test files import helpers with `from fakes import ...`, so `python -m unittest tests.<module>` from the repository root fails.
