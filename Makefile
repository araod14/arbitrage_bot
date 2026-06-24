# Makefile para desarrollo y despliegue del bot de arbitraje P2P.
# Ejecuta `make` o `make help` para ver los objetivos disponibles.

VENV ?= venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help venv install test run lint clean env \
        docker-build docker-up docker-down docker-logs docker-run docker-shell

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Desarrollo local --------------------------------------------------------

venv: ## Crea el entorno virtual
	python -m venv $(VENV)

install: ## Instala dependencias y el paquete (editable)
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

test: ## Corre la batería de tests
	$(PY) -m pytest

run: ## Ejecuta el bot localmente (interactivo)
	$(PY) -m p2p_arb_bot.main

env: ## Crea .env desde .env.example si no existe
	@test -f .env || (cp .env.example .env && echo ".env creado desde .env.example")

clean: ## Borra cachés y artefactos de build
	rm -rf .pytest_cache .mypy_cache build dist src/*.egg-info
	find . -type d -name __pycache__ -not -path './venv/*' -exec rm -rf {} +

# --- Docker / despliegue -----------------------------------------------------

docker-build: ## Construye la imagen
	docker compose build

docker-up: env ## Levanta el bot en segundo plano (NO_INPUT=true)
	docker compose up -d --build

docker-down: ## Detiene y elimina el contenedor
	docker compose down

docker-logs: ## Sigue los logs del contenedor
	docker compose logs -f

docker-run: env ## Corre en primer plano de forma interactiva (descubrir métodos)
	docker compose run --rm -e NO_INPUT=false bot

docker-shell: ## Abre una shell dentro del contenedor
	docker compose run --rm --entrypoint sh bot
