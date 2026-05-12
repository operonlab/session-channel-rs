# session-channel — common dev tasks
.PHONY: help install dev test lint run docker docker-compose-up docker-compose-down clean

SESSION_CHANNEL_HOME ?= $(HOME)/.session-channel
PY ?= python3
PORT ?= 10101

help:
	@echo "Targets:"
	@echo "  install            Install via ./install.sh (uses SESSION_CHANNEL_HOME=$(SESSION_CHANNEL_HOME))"
	@echo "  dev                Editable install in a local .venv"
	@echo "  test               Run pytest (requires fakeredis + freezegun + pytest-asyncio)"
	@echo "  lint               ruff check"
	@echo "  run                Start uvicorn on PORT=$(PORT)"
	@echo "  docker             Build Docker image (dist/Dockerfile)"
	@echo "  docker-compose-up  docker compose up -d (Redis + session-channel)"
	@echo "  docker-compose-down  docker compose down"
	@echo "  clean              Remove __pycache__ + .pytest_cache"

install:
	./install.sh

dev:
	$(PY) -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -e ".[dev]"

test:
	$(PY) -m pytest tests/ -v

lint:
	ruff check .

run:
	uvicorn main:app --host 127.0.0.1 --port $(PORT) --reload

docker:
	docker build -f dist/Dockerfile -t session-channel:latest .

docker-compose-up:
	docker compose -f dist/docker-compose.yml up -d

docker-compose-down:
	docker compose -f dist/docker-compose.yml down

clean:
	find . -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
