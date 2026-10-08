.PHONY: test replay deploy cost-report

test:
	uv run --locked --all-packages ruff check .
	uv run --locked --all-packages ruff format --check .
	uv run --locked --all-packages pytest

replay:
	uv run --locked --all-packages python -m tripvane_sensors.runtime.replay fixtures

deploy:
	@echo "deploy: not implemented (SENSOR=$(SENSOR) HOST=$(HOST))"
	@exit 1

cost-report:
	@echo "cost-report: not implemented"
	@exit 1
