import os
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import git
from dotenv import load_dotenv
from datetime import datetime

def fetch_spotify_data():
    # Spotify Authentication
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope="user-read-recently-played"))

    # Fetch recent tracks
    results = sp.current_user_recently_played(limit=50)  # Adjust the limit as needed
    return results

from datetime import datetime

def save_to_json(data, filename='spotify_data.json'):
    simplified_data = []
    for item in data['items']:
        track = item['track']
        played_at = item['played_at']

        # Check if 'played_at' contains microseconds
        if '.' not in played_at:
            # If no microseconds, use a format string without '%f'
            played_at_dt = datetime.strptime(played_at, '%Y-%m-%dT%H:%M:%SZ')
        else:
            # If microseconds are present, include '%f' in the format string
            played_at_dt = datetime.strptime(played_at, '%Y-%m-%dT%H:%M:%S.%fZ')

        formatted_played_at = played_at_dt.strftime('%d/%m/%Y - %H:%M:%S')

        track_info = {
            'song_name': track['name'],
            'artists': [artist['name'] for artist in track['artists']],
            'album': track['album']['name'],
            'played_at': formatted_played_at
        }
        simplified_data.append(track_info)

    with open(filename, 'w', encoding='utf-8') as outfile:
        json.dump(simplified_data, outfile, ensure_ascii=False, indent=4)


def git_commit_and_push(repo_dir, filename):
    # Git operations
    repo = git.Repo(repo_dir)
    repo.git.add(filename)
    repo.index.commit('Update listening history')
    origin = repo.remote(name='origin')
    origin.push()

def main():
    # Load environment variables
    load_dotenv()

    # Fetch Spotify Data
    spotify_data = fetch_spotify_data()

    # Save to JSON
    save_to_json(spotify_data)

    # Git commit and push
    repo_dir = os.getcwd()  # Gets the current working directory
    git_commit_and_push(repo_dir, 'spotify_data.json')

if __name__ == '__main__':
    main()
