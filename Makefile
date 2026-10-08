.PHONY: test replay deploy deploy-core cost-report

test:
	uv run --locked --all-packages ruff check .
	uv run --locked --all-packages ruff format --check .
	uv run --locked --all-packages pytest

replay:
	uv run --locked --all-packages python -m tripvane_sensors.runtime.replay fixtures
	uv run --locked --all-packages python -m tripvane_sensors.archetypes.github.replay fixtures/github

deploy:
	@if [ -z "$(SENSOR)" ] || [ -z "$(HOST)" ]; then \
		echo "usage: make deploy SENSOR=support HOST=1.2.3.4" >&2; exit 2; fi
	infra/deploy.sh "$(SENSOR)" "$(HOST)"

deploy-core:
	@if [ -z "$(HOST)" ]; then \
		echo "usage: make deploy-core HOST=1.2.3.4" >&2; exit 2; fi
	infra/deploy-core.sh "$(HOST)"

cost-report:
	@echo "cost-report: not implemented"
	@exit 1
