.PHONY: help install test coverage lint format translate run dev clean publish

.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

install: ## Install dependencies and pre-commit hooks
	uv sync --all-extras
	git config --local --unset-all core.hooksPath 2>/dev/null || true
	git config --global --unset-all core.hooksPath 2>/dev/null || true
	uv run pre-commit install

test: ## Run tests
	uv run --all-extras pytest tests/ -v -n auto

coverage: ## Run tests with coverage report (terminal + HTML)
	uv run --all-extras pytest tests/ -n auto --cov --cov-report=term-missing --cov-report=html
	@echo "HTML report: htmlcov/index.html"

lint: ## Run linters
	uv run ruff check src/ tests/
	uv run ty check src/

format: ## Format code
	uv run ruff format src/ tests/

LOCALES := zh es fr de ja pt
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/iac_code/__init__.py)

translate: ## Extract, update and compile translations
	@uv run pybabel extract -F babel.cfg --project=iac-code --version=$(VERSION) -o src/iac_code/i18n/messages.pot . > /dev/null 2>&1 && echo "Extract: OK" || (echo "Extract: FAILED"; exit 1)
	@for lang in $(LOCALES); do \
		uv run pybabel update -i src/iac_code/i18n/messages.pot -d src/iac_code/i18n/locales -l $$lang > /dev/null 2>&1 && echo "Update  $$lang: OK" || (echo "Update  $$lang: FAILED"; exit 1); \
	done
	@for lang in $(LOCALES); do \
		perl -i -pe 's/^"Project-Id-Version: .*/"Project-Id-Version: iac-code $(VERSION)\\n"/' src/iac_code/i18n/locales/$$lang/LC_MESSAGES/messages.po; \
	done
	@for lang in $(LOCALES); do \
		uv run pybabel compile -d src/iac_code/i18n/locales -l $$lang > /dev/null 2>&1 && echo "Compile $$lang: OK" || (echo "Compile $$lang: FAILED"; exit 1); \
	done

run: ## Run iac-code
	uv run iac-code

dev: ## Run iac-code in debug mode
	uv run iac-code --debug

clean: ## Clean build artifacts
	rm -rf .ruff_cache .pytest_cache dist build htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pot" -delete
	find . -type f -name "*.mo" -delete
	find . -type f -name ".coverage.*" -delete
