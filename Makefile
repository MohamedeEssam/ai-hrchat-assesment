.PHONY: up up-build down logs test test-build reset-demo

# Start the app in the background (http://localhost:8001). Data is kept between restarts.
up:
	docker compose up -d

# Rebuild the image first (after changing requirements.txt or the Dockerfile).
up-build:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f backend

# Run all tests in their own database (hrchat_test). No API key needed.
test:
	docker compose run --rm tests

test-build:
	docker compose run --build --rm tests

# Wipe the demo database and load the seed data again (balances back to 12 / 10 / 5).
reset-demo:
	docker compose exec backend python scripts/seed.py --reset
