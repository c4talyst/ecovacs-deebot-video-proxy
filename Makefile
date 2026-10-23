.PHONY: help build dev up down restart logs test clean

help:
	@echo "Ecovacs Deebot Video Proxy - Developer Commands:"
	@echo "  make dev      - Build and run in foreground with live code mounting (Ctrl+C to stop)"
	@echo "  make up       - Build and run in background (detached)"
	@echo "  make down     - Stop and remove the development container"
	@echo "  make restart  - Restart the container (applies Python edits instantly)"
	@echo "  make logs     - Follow container logs in real time"
	@echo "  make build    - Build the Docker image locally"
	@echo "  make clean    - Remove build cache and temp containers"

dev:
	@mkdir -p config
	docker compose -f docker-compose.dev.yml up --build

up:
	@mkdir -p config
	docker compose -f docker-compose.dev.yml up -d --build

down:
	docker compose -f docker-compose.dev.yml down

restart:
	docker compose -f docker-compose.dev.yml restart

logs:
	docker compose -f docker-compose.dev.yml logs -f --tail=100

build:
	docker compose -f docker-compose.dev.yml build

clean:
	docker compose -f docker-compose.dev.yml down -v --remove-orphans
