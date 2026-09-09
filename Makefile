.PHONY: install dev worker migrate revision test lint fmt smoke bakeoff

install:
	pip install -e ".[dev]" && playwright install chromium

dev:
	uvicorn app.main:app --reload --port 8000

worker:
	python -m app.queue.worker

migrate:
	alembic upgrade head

revision:
	alembic revision -m "$(m)"

test:
	pytest -q

lint:
	ruff check app scripts tests

fmt:
	ruff check --fix app scripts tests

smoke:
	python scripts/journey_smoke.py

bakeoff:
	python scripts/stt_bakeoff.py --providers elevenlabs,deepgram,sarvam
