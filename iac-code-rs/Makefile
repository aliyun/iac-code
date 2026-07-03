.PHONY: help install build test coverage lint format run dev clean

.DEFAULT_GOAL := help

REPO_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)
CARGO_TARGET_DIR ?= $(CURDIR)/target
BIN_PATH := $(abspath $(CARGO_TARGET_DIR)/release/iac-code)
ARGS ?=

help: ## Show this help message
	@echo "Usage: make [target] [ARGS=\"...\"]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install Rust dependencies, coverage tool, and pre-commit hooks
	rustup component add rustfmt clippy
	cargo fetch
	@if ! cargo llvm-cov --version >/dev/null 2>&1; then \
		cargo install cargo-llvm-cov --locked; \
	fi
	@if command -v pre-commit >/dev/null 2>&1; then \
		cd $(REPO_ROOT) && pre-commit install; \
	elif command -v uv >/dev/null 2>&1; then \
		cd $(REPO_ROOT) && uv run pre-commit install; \
	else \
		echo "pre-commit is not installed; install it or run from the repo root with uv available."; \
		exit 1; \
	fi

build: ## Build the release binary
	cargo build --release -p iac-code-cli --bin iac-code
	@echo "Binary: $(BIN_PATH)"

test: ## Run tests
	cargo test

coverage: ## Run tests with coverage
	@if ! cargo llvm-cov --version >/dev/null 2>&1; then \
		echo "cargo-llvm-cov is required. Install it with: cargo install cargo-llvm-cov --locked"; \
		exit 1; \
	fi
	cargo llvm-cov --workspace --all-features --html
	@echo "HTML report: target/llvm-cov/html/index.html"

lint: ## Run linters
	cargo clippy --tests -- -D warnings

format: ## Format code
	cargo fmt --all

run: ## Run iac-code locally (ARGS="...")
	cargo run -p iac-code-cli -- $(ARGS)

dev: ## Run iac-code in debug mode (ARGS="...")
	RUST_LOG=debug cargo run -p iac-code-cli -- --debug $(ARGS)

clean: ## Clean build artifacts
	cargo clean
	rm -rf target/llvm-cov
