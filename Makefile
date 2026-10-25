PYTHON ?= python3

.PHONY: help run test lint check install uninstall clean

help:
	@echo "make run        Run the TUI (dry run)"
	@echo "make test       Run the test suite"
	@echo "make lint       Run ruff"
	@echo "make check      Compile and run the tests"
	@echo "make install    Install the mac-clean command with pipx"
	@echo "make uninstall  Remove the pipx installation"
	@echo "make clean      Remove build and cache artifacts"

run:
	$(PYTHON) mac_clean.py

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m ruff check .

check:
	$(PYTHON) -m py_compile mac_clean.py
	$(PYTHON) -m unittest discover -s tests -v

install:
	@command -v pipx >/dev/null 2>&1 || { echo "pipx not found. Install it with: brew install pipx"; exit 1; }
	pipx install --force .

uninstall:
	pipx uninstall mac-clean

clean:
	rm -rf build dist *.egg-info .ruff_cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
