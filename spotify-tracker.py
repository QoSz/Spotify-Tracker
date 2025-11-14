import os
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import git
from dotenv import load_dotenv
from datetime import datetime
import pytz
import sqlite3

def initialize_database():
    conn = sqlite3.connect('play_counter.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS play_count
                 (id INTEGER PRIMARY KEY, count INTEGER)''')
    c.execute('SELECT count FROM play_count WHERE id=1')
    if c.fetchone() is None:
        c.execute('INSERT INTO play_count (id, count) VALUES (1, 0)')
    conn.commit()
    conn.close()

def load_play_count():
    conn = sqlite3.connect('play_counter.db')
    c = conn.cursor()
    c.execute('SELECT count FROM play_count WHERE id=1')
    count = c.fetchone()[0]
    conn.close()
    return count

def save_play_count(count):
    conn = sqlite3.connect('play_counter.db')
    c = conn.cursor()
    c.execute('UPDATE play_count SET count = ? WHERE id = 1', (count,))
    conn.commit()
    conn.close()

def fetch_spotify_data():
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope="user-read-recently-played"))
    return sp.current_user_recently_played(limit=50)

def save_to_json(data, play_count, filename='spotify_data.json'):
    try:
        with open(filename, 'r', encoding='utf-8') as infile:
            existing_data = json.load(infile)
            simplified_data = existing_data.get('tracks', [])
    except (FileNotFoundError, json.JSONDecodeError):
        simplified_data = []

    london_tz = pytz.timezone('Europe/London')

    # Create a set of existing track signatures to avoid duplicates
    existing_signatures = {
        (track['song_name'], track['album'], tuple(track['artists']), track['played_at'])
        for track in simplified_data
    }

    new_tracks_count = 0

    for item in data['items']:
        track = item['track']
        played_at = item['played_at']
        try:
            played_at_dt = datetime.strptime(played_at, '%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError:
            played_at_dt = datetime.strptime(played_at, '%Y-%m-%dT%H:%M:%SZ')

        played_at_dt = pytz.utc.localize(played_at_dt).astimezone(london_tz)
        formatted_played_at = played_at_dt.strftime('%d/%m/%Y - %H:%M:%S')

        track_info = {
            'song_name': track['name'],
            'artists': [artist['name'] for artist in track['artists']],
            'album': track['album']['name'],
            'played_at': formatted_played_at
        }

        # Create signature for this track
        track_signature = (
            track_info['song_name'],
            track_info['album'],
            tuple(track_info['artists']),
            track_info['played_at']
        )

        # Only add if not already present
        if track_signature not in existing_signatures:
            simplified_data.append(track_info)
            existing_signatures.add(track_signature)
            new_tracks_count += 1

    # Keep only the last 200 tracks
    simplified_data = simplified_data[-200:]

    # Update play count with new tracks
    updated_play_count = play_count + new_tracks_count

    with open(filename, 'w', encoding='utf-8') as outfile:
        # Include the play count in the JSON file
        json.dump({'total_plays': updated_play_count, 'tracks': simplified_data}, outfile, ensure_ascii=False, indent=4)

    save_play_count(updated_play_count)



def git_commit_and_push(repo_dir, filename):
    repo = git.Repo(repo_dir)
    repo.git.add(filename)
    repo.index.commit('Update listening history')
    origin = repo.remote(name='origin')
    origin.push()

def main():
    load_dotenv()
    initialize_database()  # Ensure database is set up
    play_count = load_play_count()
    spotify_data = fetch_spotify_data()
    save_to_json(spotify_data, play_count, 'spotify_data.json')
    git_commit_and_push(os.getcwd(), 'spotify_data.json')

if __name__ == '__main__':
    main()
