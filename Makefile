PYTHON ?= .venv/bin/python
MATURIN ?= .venv/bin/maturin

.PHONY: venv install develop test layout-conformance check clean

venv:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install maturin

install:
	$(PYTHON) -m pip install -e .

develop:
	$(MATURIN) develop --release

test:
	$(PYTHON) -m pytest tests

layout-conformance:
	PYTHONPATH=tests/layout $(PYTHON) -m harness.run_suite

layout-wpt-server:
	$(PYTHON) -m http.server 8943 --directory tests/wpt --bind 127.0.0.1

check:
	cargo check
	$(PYTHON) -m pytest tests -q

clean:
	cargo clean
