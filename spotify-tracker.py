"""
Spotify Tracker - Track your listening history with analytics.

A personal automation tool that fetches Spotify listening history,
stores it locally with full analytics, and backs it up to GitHub.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Generator
from zoneinfo import ZoneInfo

import git
import spotipy
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from spotipy.oauth2 import SpotifyOAuth

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    spotipy_client_id: str
    spotipy_client_secret: str
    spotipy_redirect_uri: str
    timezone: str = "Europe/London"
    max_history_size: int = 200
    spotify_fetch_limit: int = 50
    database_file: str = "play_counter.db"
    data_file: str = "spotify_data.json"
    readme_file: str = "README.md"
    spotify_scope: str = "user-read-recently-played"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# =============================================================================
# Data Models
# =============================================================================

@dataclass(frozen=True, slots=True)
class Track:
    """Represents a played track."""

    song_name: str
    artists: list[str]
    album: str
    played_at: datetime
    played_at_formatted: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'song_name': self.song_name,
            'artists': self.artists,
            'album': self.album,
            'played_at': self.played_at_formatted
        }


@dataclass
class ListeningStats:
    """Comprehensive listening statistics."""

    total_plays: int
    unique_artists: int
    unique_albums: int
    unique_songs: int
    top_artists: list[tuple[str, int]]
    top_albums: list[tuple[str, int]]
    top_songs: list[tuple[str, int]]
    current_streak: int
    longest_streak: int
    daily_average: float
    plays_by_hour: dict[int, int]
    plays_by_weekday: dict[str, int]


@dataclass
class DailyStats:
    """Statistics for a single day."""

    date: str
    play_count: int
    unique_artists: int
    unique_songs: int


# =============================================================================
# Exceptions
# =============================================================================

class SpotifyTrackerError(Exception):
    """Base exception with context."""

    def __init__(self, message: str, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}


class ConfigurationError(SpotifyTrackerError):
    """Raised when required configuration is missing."""
    pass


class SpotifyAPIError(SpotifyTrackerError):
    """Spotify API specific errors."""
    pass


class DatabaseError(SpotifyTrackerError):
    """Database operation errors."""
    pass


# =============================================================================
# Database Operations
# =============================================================================

@contextmanager
def get_db_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections ensuring proper cleanup."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def initialize_database(db_path: str) -> None:
    """Initialize the database with all required tables."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Original play_count table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS play_count
            (id INTEGER PRIMARY KEY, count INTEGER)
        ''')
        cursor.execute('SELECT count FROM play_count WHERE id = 1')
        if cursor.fetchone() is None:
            cursor.execute('INSERT INTO play_count (id, count) VALUES (1, 0)')
            logger.info("Initialized play count with 0")

        # New tracks table for full history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_name TEXT NOT NULL,
                artists TEXT NOT NULL,
                album TEXT NOT NULL,
                played_at TEXT NOT NULL UNIQUE,
                played_at_date TEXT NOT NULL,
                played_at_hour INTEGER NOT NULL,
                played_at_weekday TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Index for faster queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tracks_date
            ON tracks(played_at_date)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tracks_artists
            ON tracks(artists)
        ''')

        conn.commit()


def migrate_json_to_database(settings: Settings) -> int:
    """
    Migrate existing tracks from JSON file to database.
    Returns the number of tracks migrated.
    """
    json_path = settings.data_file
    if not os.path.exists(json_path):
        return 0

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return 0

    tracks = data.get('tracks', [])
    if not tracks:
        return 0

    migrated = 0
    tz = ZoneInfo(settings.timezone)

    with get_db_connection(settings.database_file) as conn:
        cursor = conn.cursor()

        for track in tracks:
            played_at_str = track.get('played_at', '')
            if not played_at_str:
                continue

            # Parse the formatted timestamp back to datetime
            try:
                dt = datetime.strptime(played_at_str, '%d/%m/%Y - %H:%M:%S')
                dt = dt.replace(tzinfo=tz)
            except ValueError:
                continue

            played_at_date = dt.strftime('%Y-%m-%d')
            played_at_hour = dt.hour
            played_at_weekday = dt.strftime('%A')
            artists_json = json.dumps(track.get('artists', []))

            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO tracks
                    (song_name, artists, album, played_at, played_at_date,
                     played_at_hour, played_at_weekday)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    track.get('song_name', ''),
                    artists_json,
                    track.get('album', ''),
                    played_at_str,
                    played_at_date,
                    played_at_hour,
                    played_at_weekday
                ))
                if cursor.rowcount > 0:
                    migrated += 1
            except sqlite3.IntegrityError:
                continue

        conn.commit()

    if migrated > 0:
        logger.info(f"Migrated {migrated} tracks from JSON to database")

    return migrated


def load_play_count(db_path: str) -> int:
    """Load the current play count from the database."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT count FROM play_count WHERE id = 1')
        result = cursor.fetchone()
        return result[0] if result else 0


def save_play_count(count: int, db_path: str) -> None:
    """Save the updated play count to the database."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE play_count SET count = ? WHERE id = 1', (count,))
        conn.commit()


def insert_track(track: Track, db_path: str) -> bool:
    """
    Insert a track into the database.
    Returns True if inserted, False if duplicate.
    """
    artists_json = json.dumps(track.artists)
    played_at_date = track.played_at.strftime('%Y-%m-%d')
    played_at_hour = track.played_at.hour
    played_at_weekday = track.played_at.strftime('%A')

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO tracks
                (song_name, artists, album, played_at, played_at_date,
                 played_at_hour, played_at_weekday)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                track.song_name,
                artists_json,
                track.album,
                track.played_at_formatted,
                played_at_date,
                played_at_hour,
                played_at_weekday
            ))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            return False


# =============================================================================
# Analytics Functions
# =============================================================================

def get_top_artists(db_path: str, limit: int = 10) -> list[tuple[str, int]]:
    """Get most played artists with play counts."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT artists FROM tracks')
        rows = cursor.fetchall()

    artist_counts: Counter[str] = Counter()
    for row in rows:
        artists = json.loads(row['artists'])
        for artist in artists:
            artist_counts[artist] += 1

    return artist_counts.most_common(limit)


def get_top_albums(db_path: str, limit: int = 10) -> list[tuple[str, int]]:
    """Get most played albums with play counts."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT album, COUNT(*) as count
            FROM tracks
            GROUP BY album
            ORDER BY count DESC
            LIMIT ?
        ''', (limit,))
        return [(row['album'], row['count']) for row in cursor.fetchall()]


def get_top_songs(db_path: str, limit: int = 10) -> list[tuple[str, int]]:
    """Get most played songs with play counts."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT song_name, artists, COUNT(*) as count
            FROM tracks
            GROUP BY song_name, artists
            ORDER BY count DESC
            LIMIT ?
        ''', (limit,))
        results = []
        for row in cursor.fetchall():
            artists = json.loads(row['artists'])
            display = f"{row['song_name']} - {', '.join(artists)}"
            results.append((display, row['count']))
        return results


def get_listening_streak(db_path: str) -> tuple[int, int]:
    """
    Get current and longest listening streaks (days).
    Returns (current_streak, longest_streak).
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT played_at_date
            FROM tracks
            ORDER BY played_at_date DESC
        ''')
        dates = [row['played_at_date'] for row in cursor.fetchall()]

    if not dates:
        return 0, 0

    # Parse dates
    date_set = {datetime.strptime(d, '%Y-%m-%d').date() for d in dates}
    sorted_dates = sorted(date_set, reverse=True)

    # Calculate current streak
    today = datetime.now().date()
    current_streak = 0
    check_date = today

    # Allow for today or yesterday to count
    if check_date not in date_set:
        check_date = today - timedelta(days=1)

    while check_date in date_set:
        current_streak += 1
        check_date -= timedelta(days=1)

    # Calculate longest streak
    longest_streak = 0
    current_run = 0
    prev_date = None

    for d in sorted(date_set):
        if prev_date is None or (d - prev_date).days == 1:
            current_run += 1
        else:
            longest_streak = max(longest_streak, current_run)
            current_run = 1
        prev_date = d

    longest_streak = max(longest_streak, current_run)

    return current_streak, longest_streak


def get_plays_by_hour(db_path: str) -> dict[int, int]:
    """Get play counts grouped by hour of day (0-23)."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT played_at_hour, COUNT(*) as count
            FROM tracks
            GROUP BY played_at_hour
            ORDER BY played_at_hour
        ''')
        return {row['played_at_hour']: row['count'] for row in cursor.fetchall()}


def get_plays_by_weekday(db_path: str) -> dict[str, int]:
    """Get play counts grouped by day of week."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT played_at_weekday, COUNT(*) as count
            FROM tracks
            GROUP BY played_at_weekday
        ''')
        return {row['played_at_weekday']: row['count'] for row in cursor.fetchall()}


def get_unique_counts(db_path: str) -> tuple[int, int, int]:
    """Get counts of unique artists, albums, and songs."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Unique artists (need to parse JSON)
        cursor.execute('SELECT artists FROM tracks')
        all_artists = set()
        for row in cursor.fetchall():
            artists = json.loads(row['artists'])
            all_artists.update(artists)

        # Unique albums
        cursor.execute('SELECT COUNT(DISTINCT album) FROM tracks')
        unique_albums = cursor.fetchone()[0]

        # Unique songs (by name + artists combo)
        cursor.execute('''
            SELECT COUNT(DISTINCT song_name || artists) FROM tracks
        ''')
        unique_songs = cursor.fetchone()[0]

    return len(all_artists), unique_albums, unique_songs


def get_daily_average(db_path: str) -> float:
    """Calculate average plays per day."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) as total,
                   COUNT(DISTINCT played_at_date) as days
            FROM tracks
        ''')
        row = cursor.fetchone()
        if row and row['days'] > 0:
            return round(row['total'] / row['days'], 1)
        return 0.0


def generate_stats_summary(db_path: str) -> ListeningStats:
    """Generate comprehensive listening statistics."""
    total_plays = load_play_count(db_path)
    unique_artists, unique_albums, unique_songs = get_unique_counts(db_path)
    top_artists = get_top_artists(db_path, limit=5)
    top_albums = get_top_albums(db_path, limit=5)
    top_songs = get_top_songs(db_path, limit=5)
    current_streak, longest_streak = get_listening_streak(db_path)
    daily_average = get_daily_average(db_path)
    plays_by_hour = get_plays_by_hour(db_path)
    plays_by_weekday = get_plays_by_weekday(db_path)

    return ListeningStats(
        total_plays=total_plays,
        unique_artists=unique_artists,
        unique_albums=unique_albums,
        unique_songs=unique_songs,
        top_artists=top_artists,
        top_albums=top_albums,
        top_songs=top_songs,
        current_streak=current_streak,
        longest_streak=longest_streak,
        daily_average=daily_average,
        plays_by_hour=plays_by_hour,
        plays_by_weekday=plays_by_weekday
    )


# =============================================================================
# Spotify API Operations
# =============================================================================

def fetch_spotify_data(settings: Settings) -> dict:
    """Fetch recently played tracks from Spotify API."""
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope=settings.spotify_scope))
        data = sp.current_user_recently_played(limit=settings.spotify_fetch_limit)
        logger.info(f"Fetched {len(data.get('items', []))} tracks from Spotify")
        return data
    except spotipy.SpotifyException as e:
        raise SpotifyAPIError(
            f"Failed to fetch Spotify data: {e}",
            context={'error': str(e)}
        ) from e


def parse_timestamp(played_at: str) -> datetime:
    """Parse Spotify timestamp to datetime, handling both formats."""
    for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(played_at, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unable to parse timestamp: {played_at}")


def process_track(item: dict, tz: ZoneInfo) -> Track:
    """Extract and format track information from Spotify API response item."""
    track_data = item['track']
    played_at_str = item['played_at']
    played_at_utc = parse_timestamp(played_at_str)
    played_at_local = played_at_utc.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
    formatted_time = played_at_local.strftime('%d/%m/%Y - %H:%M:%S')

    return Track(
        song_name=track_data['name'],
        artists=[artist['name'] for artist in track_data['artists']],
        album=track_data['album']['name'],
        played_at=played_at_local,
        played_at_formatted=formatted_time
    )


# =============================================================================
# Data Persistence
# =============================================================================

def load_existing_data(filename: str) -> tuple[list[dict], set[str]]:
    """
    Load existing track data from JSON file.
    Returns tuple of (tracks_list, existing_timestamps_set).
    """
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tracks = data.get('tracks', [])
            timestamps = {track.get('played_at') for track in tracks}
            return tracks, timestamps
    except FileNotFoundError:
        logger.info(f"No existing data file found at {filename}")
        return [], set()
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse {filename}: {e}")
        return [], set()


def save_to_json(
    spotify_data: dict,
    play_count: int,
    settings: Settings
) -> tuple[int, int]:
    """
    Process Spotify data, deduplicate, and save to JSON and database.
    Returns tuple of (new_track_count, updated_play_count).
    """
    existing_tracks, existing_timestamps = load_existing_data(settings.data_file)
    tz = ZoneInfo(settings.timezone)

    new_tracks: list[Track] = []
    for item in spotify_data.get('items', []):
        track = process_track(item, tz)
        if track.played_at_formatted not in existing_timestamps:
            new_tracks.append(track)
            existing_timestamps.add(track.played_at_formatted)
            # Also insert into database
            insert_track(track, settings.database_file)

    if not new_tracks:
        logger.info("No new tracks to add")
        return 0, play_count

    # Add new tracks and maintain size limit for JSON file
    all_tracks = existing_tracks + [t.to_dict() for t in new_tracks]
    all_tracks = all_tracks[-settings.max_history_size:]

    # Update play count
    updated_count = play_count + len(new_tracks)

    # Save to JSON file
    output_data = {
        'total_plays': updated_count,
        'tracks': all_tracks
    }
    with open(settings.data_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)

    # Persist play count to database
    save_play_count(updated_count, settings.database_file)

    logger.info(f"Added {len(new_tracks)} new tracks, total: {updated_count}")

    return len(new_tracks), updated_count


# =============================================================================
# README Generation
# =============================================================================

def update_readme(settings: Settings) -> None:
    """Update the README file with current statistics."""
    stats = generate_stats_summary(settings.database_file)

    # Format top artists
    top_artists_lines = []
    for i, (artist, count) in enumerate(stats.top_artists, 1):
        top_artists_lines.append(f"{i}. {artist} ({count} plays)")

    # Find peak listening hour
    peak_hour = max(stats.plays_by_hour.items(), key=lambda x: x[1])[0] if stats.plays_by_hour else 0
    peak_hour_formatted = f"{peak_hour:02d}:00"

    content = f"""# Spotify Tracker

Tracking my Spotify listening history.

## Lifetime Stats

**{stats.total_plays:,}** songs listened to

- **{stats.unique_artists}** unique artists
- **{stats.unique_albums}** unique albums
- **{stats.unique_songs}** unique songs
- **{stats.daily_average}** songs per day (average)

## Top Artists (All Time)

{chr(10).join(top_artists_lines) if top_artists_lines else "No data yet"}

## Listening Streak

- Current: **{stats.current_streak}** days
- Longest: **{stats.longest_streak}** days

## Listening Patterns

- Peak hour: **{peak_hour_formatted}**

---
*Automatically updated by [spotify-tracker.py](spotify-tracker.py)*
"""
    with open(settings.readme_file, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info("Updated README with statistics")


# =============================================================================
# Git Operations
# =============================================================================

def git_commit_and_push(repo_dir: str, files: list[str]) -> bool:
    """
    Commit and push changes to git repository.
    Returns True if changes were committed, False if no changes.
    """
    try:
        repo = git.Repo(repo_dir)
        untracked = set(repo.untracked_files)

        has_changes = any(
            repo.is_dirty(path=filename) or filename in untracked
            for filename in files
        )

        if not has_changes:
            logger.info("No changes to commit")
            return False

        repo.git.add(files)
        repo.index.commit('Update listening history')
        origin = repo.remote(name='origin')
        origin.push()

        logger.info("Successfully committed and pushed changes")
        return True

    except git.GitCommandError as e:
        raise SpotifyTrackerError(
            f"Git operation failed: {e}",
            context={'error': str(e)}
        ) from e


# =============================================================================
# CLI Display Functions
# =============================================================================

def display_stats(settings: Settings) -> int:
    """Display comprehensive listening statistics."""
    stats = generate_stats_summary(settings.database_file)

    print("\n" + "=" * 50)
    print("SPOTIFY LISTENING STATISTICS")
    print("=" * 50)

    print(f"\n📊 Lifetime Stats")
    print(f"   Total plays: {stats.total_plays:,}")
    print(f"   Unique artists: {stats.unique_artists}")
    print(f"   Unique albums: {stats.unique_albums}")
    print(f"   Unique songs: {stats.unique_songs}")
    print(f"   Daily average: {stats.daily_average}")

    print(f"\n🔥 Listening Streak")
    print(f"   Current: {stats.current_streak} days")
    print(f"   Longest: {stats.longest_streak} days")

    print(f"\n🎤 Top Artists")
    for i, (artist, count) in enumerate(stats.top_artists, 1):
        print(f"   {i}. {artist} ({count} plays)")

    print(f"\n💿 Top Albums")
    for i, (album, count) in enumerate(stats.top_albums, 1):
        print(f"   {i}. {album} ({count} plays)")

    print(f"\n🎵 Top Songs")
    for i, (song, count) in enumerate(stats.top_songs, 1):
        print(f"   {i}. {song} ({count} plays)")

    if stats.plays_by_hour:
        peak_hour = max(stats.plays_by_hour.items(), key=lambda x: x[1])
        print(f"\n⏰ Peak Listening Hour: {peak_hour[0]:02d}:00 ({peak_hour[1]} plays)")

    print("\n" + "=" * 50)
    return 0


def display_top_artists(settings: Settings, limit: int) -> int:
    """Display top artists."""
    top = get_top_artists(settings.database_file, limit)
    print(f"\n🎤 Top {limit} Artists")
    print("-" * 40)
    for i, (artist, count) in enumerate(top, 1):
        print(f"{i:2}. {artist} ({count} plays)")
    return 0


def display_top_albums(settings: Settings, limit: int) -> int:
    """Display top albums."""
    top = get_top_albums(settings.database_file, limit)
    print(f"\n💿 Top {limit} Albums")
    print("-" * 40)
    for i, (album, count) in enumerate(top, 1):
        print(f"{i:2}. {album} ({count} plays)")
    return 0


def display_top_songs(settings: Settings, limit: int) -> int:
    """Display top songs."""
    top = get_top_songs(settings.database_file, limit)
    print(f"\n🎵 Top {limit} Songs")
    print("-" * 40)
    for i, (song, count) in enumerate(top, 1):
        print(f"{i:2}. {song} ({count} plays)")
    return 0


def display_streak(settings: Settings) -> int:
    """Display listening streak information."""
    current, longest = get_listening_streak(settings.database_file)
    print(f"\n🔥 Listening Streak")
    print("-" * 40)
    print(f"   Current streak: {current} days")
    print(f"   Longest streak: {longest} days")
    return 0


# =============================================================================
# Main Entry Point
# =============================================================================

def sync_spotify(settings: Settings) -> int:
    """Sync with Spotify and update all data."""
    try:
        # Initialize database
        initialize_database(settings.database_file)

        # Migrate existing JSON data if needed
        migrate_json_to_database(settings)

        # Load current play count
        play_count = load_play_count(settings.database_file)
        logger.info(f"Starting with play count: {play_count}")

        # Fetch data from Spotify
        spotify_data = fetch_spotify_data(settings)

        # Process and save data
        new_tracks, updated_count = save_to_json(spotify_data, play_count, settings)

        # Update README and commit if there are new tracks
        if new_tracks > 0:
            update_readme(settings)
            git_commit_and_push(
                os.getcwd(),
                [settings.data_file, settings.readme_file]
            )
        else:
            logger.info("Skipping updates - no new tracks")

        logger.info("Spotify tracker completed successfully")
        return 0

    except SpotifyTrackerError as e:
        logger.error(f"Tracker error: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


def main() -> int:
    """Main entry point with CLI argument parsing."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Spotify Tracker - Track and analyze your listening history"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show comprehensive listening statistics"
    )
    parser.add_argument(
        "--top-artists", type=int, metavar="N",
        help="Show top N artists"
    )
    parser.add_argument(
        "--top-albums", type=int, metavar="N",
        help="Show top N albums"
    )
    parser.add_argument(
        "--top-songs", type=int, metavar="N",
        help="Show top N songs"
    )
    parser.add_argument(
        "--streak", action="store_true",
        help="Show listening streak information"
    )
    parser.add_argument(
        "--sync", action="store_true",
        help="Sync with Spotify (default behavior when no args)"
    )
    parser.add_argument(
        "--migrate", action="store_true",
        help="Migrate JSON data to database without syncing"
    )

    args = parser.parse_args()

    try:
        settings = Settings()
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Initialize database for all operations
    initialize_database(settings.database_file)

    # Handle CLI commands
    if args.stats:
        return display_stats(settings)
    elif args.top_artists:
        return display_top_artists(settings, args.top_artists)
    elif args.top_albums:
        return display_top_albums(settings, args.top_albums)
    elif args.top_songs:
        return display_top_songs(settings, args.top_songs)
    elif args.streak:
        return display_streak(settings)
    elif args.migrate:
        count = migrate_json_to_database(settings)
        print(f"Migrated {count} tracks to database")
        return 0
    else:
        # Default: sync with Spotify
        return sync_spotify(settings)


if __name__ == '__main__':
    sys.exit(main())
