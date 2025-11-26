# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Spotify-Tracker is a personal automation tool that fetches your Spotify listening history, stores it locally with full analytics, and backs it up to GitHub. It's designed to run periodically (via cron or scheduler) and provides CLI commands for viewing listening statistics.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the tracker (sync with Spotify - default)
python spotify-tracker.py

# View comprehensive statistics
python spotify-tracker.py --stats

# View top N artists/albums/songs
python spotify-tracker.py --top-artists 10
python spotify-tracker.py --top-albums 10
python spotify-tracker.py --top-songs 10

# View listening streak
python spotify-tracker.py --streak

# Migrate JSON data to database (one-time)
python spotify-tracker.py --migrate
```

## Architecture

Single-file application (`spotify-tracker.py`) using:
- **pydantic-settings**: Type-safe configuration from environment
- **dataclasses**: Typed data models (Track, ListeningStats)
- **zoneinfo**: Modern timezone handling (stdlib)

### Execution Flow

```
load_dotenv() → Settings() → initialize_database() → migrate_json_to_database()
    → load_play_count() → fetch_spotify_data() → save_to_json() → update_readme()
    → git_commit_and_push()
```

### Data Storage

- **`spotify_data.json`**: Rolling window of last 200 tracks (song, artists, album, played_at timestamp)
- **`play_counter.db`**: SQLite database with:
  - `play_count` table: Cumulative play counter
  - `tracks` table: Full listening history for analytics (with indexes)
- **`.cache`**: Spotipy OAuth token cache (auto-managed)

### Analytics Features

- Top artists, albums, and songs (all-time)
- Listening streaks (current and longest)
- Daily average plays
- Peak listening hour
- Plays by weekday

### External Dependencies

- **Spotify Web API**: Uses `spotipy` library with OAuth scope `user-read-recently-played`
- **Git**: Uses `GitPython` to commit and push changes to remote
- **Pydantic**: Configuration validation and settings management

### Configuration

Environment variables loaded from `.env` (via pydantic-settings):
- `SPOTIPY_CLIENT_ID` (required)
- `SPOTIPY_CLIENT_SECRET` (required)
- `SPOTIPY_REDIRECT_URI` (required)
- `TIMEZONE` (optional, default: "Europe/London")
- `MAX_HISTORY_SIZE` (optional, default: 200)
- `DATABASE_FILE` (optional, default: "play_counter.db")
- `DATA_FILE` (optional, default: "spotify_data.json")

### Key Behaviors

- Timestamps are converted from UTC to configured timezone
- Each sync fetches up to 50 recent tracks and appends to existing history
- JSON history is capped at 200 tracks (oldest removed first)
- Database stores full history for analytics (no limit)
- Every sync with new tracks creates a git commit and pushes to origin
- README is auto-updated with top artists and listening stats
