"""
Concert Tracker
---------------
1. Puxa top 150 artistas do Last.fm
2. Busca shows em São Paulo via Ticketmaster, 30e e Eventim (Playwright)
3. Cruza artistas favoritos com shows anunciados (Setlist.fm como fallback)
4. Analisa os 10 setlists mais recentes de cada artista (frequência estatística)
5. Cria playlist no Spotify com as músicas mais prováveis
"""

import os
import sys
import time

sys.stdout.reconfigure(line_buffering=True)
import json
import unicodedata
import webbrowser
import urllib.parse
import http.server
from collections import Counter
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# ── Credenciais ──────────────────────────────────────────────────────────────
LASTFM_API_KEY        = os.environ.get("LASTFM_API_KEY")
LASTFM_USERNAME       = os.environ.get("LASTFM_USERNAME")
SETLISTFM_API_KEY     = os.environ.get("SETLISTFM_API_KEY")
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI")

_REQUIRED_VARS = [
    "LASTFM_API_KEY", "LASTFM_USERNAME", "SETLISTFM_API_KEY",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI",
]
_missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
if _missing:
    sys.exit(f"Variáveis faltando no .env: {', '.join(_missing)}")

TOP_ARTISTS_LIMIT    = 150
SETLISTS_TO_ANALYZE  = 10
MIN_SONG_APPEARANCES = 2
MAX_PLAYLISTS        = 5    # None = sem limite

# ── Sessions com retry automático ────────────────────────────────────────────

def _make_session(retries=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        respect_retry_after_header=False,
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

_spotify_session   = _make_session()
_setlistfm_session = _make_session()

# ── URI cache local ───────────────────────────────────────────────────────────

_URI_CACHE_PATH = ".spotify_uri_cache"
_uri_cache: dict[str, str | None] = {}


def _load_uri_cache():
    global _uri_cache
    if os.path.exists(_URI_CACHE_PATH):
        with open(_URI_CACHE_PATH) as f:
            _uri_cache = json.load(f)


def _save_uri_cache():
    with open(_URI_CACHE_PATH, "w") as f:
        json.dump(_uri_cache, f)
    os.chmod(_URI_CACHE_PATH, 0o600)


# ── Auth Spotify (Authorization Code) ────────────────────────────────────────

_spotify_token  = None
_token_capture  = {}

_SCRAPING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class _OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _token_capture["code"] = params["code"][0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Autenticado! Pode fechar esta aba.</h2>")

    def log_message(self, *args):
        pass


def _get_spotify_token():
    global _spotify_token

    cache_path = ".spotify_token_cache"
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("expires_at", 0) > time.time() + 60:
            _spotify_token = cached["access_token"]
            return _spotify_token
        r = _spotify_session.post("https://accounts.spotify.com/api/token", data={
            "grant_type":    "refresh_token",
            "refresh_token": cached["refresh_token"],
            "client_id":     SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        }, timeout=15)
        if r.ok:
            data = r.json()
            data.setdefault("refresh_token", cached["refresh_token"])
            data["expires_at"] = time.time() + data["expires_in"]
            with open(cache_path, "w") as f:
                json.dump(data, f)
            os.chmod(cache_path, 0o600)
            _spotify_token = data["access_token"]
            return _spotify_token

    scopes = "playlist-modify-public playlist-modify-private user-read-private"
    auth_url = (
        "https://accounts.spotify.com/authorize?"
        + urllib.parse.urlencode({
            "client_id":     SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri":  SPOTIFY_REDIRECT_URI,
            "scope":         scopes,
        })
    )
    print("\nAbrindo navegador para autenticação do Spotify...")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("127.0.0.1", 8888), _OAuthHandler)
    server.timeout = 60
    server.handle_request()

    code = _token_capture.get("code")
    if not code:
        print("Não foi possível capturar o código de autorização.")
        sys.exit(1)

    r = _spotify_session.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    data = r.json()
    data["expires_at"] = time.time() + data["expires_in"]
    with open(cache_path, "w") as f:
        json.dump(data, f)
    os.chmod(cache_path, 0o600)

    _spotify_token = data["access_token"]
    return _spotify_token


def _spotify_headers():
    return {"Authorization": f"Bearer {_get_spotify_token()}"}


# ── Last.fm ───────────────────────────────────────────────────────────────────

def get_top_artists(limit=TOP_ARTISTS_LIMIT):
    print(f"\nBuscando top {limit} artistas do Last.fm (@{LASTFM_USERNAME})...")
    artists = []
    page = 1
    per_page = 50

    while len(artists) < limit:
        r = requests.get("https://ws.audioscrobbler.com/2.0/", params={
            "method":  "user.gettopartists",
            "user":    LASTFM_USERNAME,
            "api_key": LASTFM_API_KEY,
            "format":  "json",
            "limit":   per_page,
            "page":    page,
            "period":  "overall",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        batch = data.get("topartists", {}).get("artist", [])
        if not batch:
            break
        artists.extend(batch)
        page += 1
        if len(batch) < per_page:
            break

    artists = artists[:limit]
    names = [a["name"] for a in artists]
    print(f"   {len(names)} artistas carregados.")
    return names


# ── Scraping de sites de ingressos ────────────────────────────────────────────

def _normalize(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _artist_in_texts(artist: str, texts: set[str]) -> bool:
    norm = _normalize(artist)
    return any(norm in _normalize(t) for t in texts)


def _fetch_ticketmaster_sp() -> set[str]:
    try:
        r = requests.get(
            "https://www.ticketmaster.com.br/",
            headers=_SCRAPING_HEADERS,
            timeout=20,
        )
        if not r.ok:
            return set()
        soup = BeautifulSoup(r.text, "html.parser")
        texts: set[str] = set()
        for tag in soup.find_all(["h3", "h2"]):
            t = tag.get_text(strip=True)
            if t:
                texts.add(t)
        for img in soup.find_all("img", alt=True):
            if img["alt"].strip():
                texts.add(img["alt"].strip())
        return texts
    except Exception as e:
        print(f"   Ticketmaster erro: {e}")
        return set()


def _fetch_30e_sp() -> set[str]:
    try:
        r = requests.get(
            "https://www.30e.live/pt-BR/tours/",
            headers=_SCRAPING_HEADERS,
            timeout=20,
        )
        if not r.ok:
            return set()
        soup = BeautifulSoup(r.text, "html.parser")
        texts: set[str] = set()
        for tag in soup.find_all(["h3", "h4"]):
            t = tag.get_text(strip=True)
            if t:
                texts.add(t)
        for img in soup.find_all("img", alt=True):
            if img["alt"].strip():
                texts.add(img["alt"].strip())
        return texts
    except Exception as e:
        print(f"   30e erro: {e}")
        return set()


def _fetch_eventim_sp() -> set[str]:
    texts: set[str] = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                user_agent=_SCRAPING_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="pt-BR",
                extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
            )
            page = context.new_page()
            page.goto(
                "https://www.eventim.com.br/city/sao-paulo-943/shows-musica-175/",
                timeout=30_000,
                wait_until="domcontentloaded",
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img", alt=True):
            if img["alt"].strip():
                texts.add(img["alt"].strip())
        for tag in soup.find_all(["h2", "h3", "h4"]):
            t = tag.get_text(strip=True)
            if t:
                texts.add(t)
    except Exception as e:
        print(f"   Eventim erro: {e}")
    return texts


# ── Setlist.fm ────────────────────────────────────────────────────────────────

def search_upcoming_shows_sp(artist_name):
    """Fallback: busca shows dos últimos 12 meses em São Paulo via Setlist.fm."""
    try:
        r = _setlistfm_session.get(
            "https://api.setlist.fm/rest/1.0/search/setlists",
            headers={"x-api-key": SETLISTFM_API_KEY, "Accept": "application/json",
                     "Connection": "close"},
            params={"artistName": artist_name, "cityName": "São Paulo", "p": 1},
            timeout=(5, 10),
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        setlists = data.get("setlist", [])
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = today.replace(year=today.year - 1)
        recent = []
        for s in setlists:
            try:
                date = datetime.strptime(s.get("eventDate", ""), "%d-%m-%Y")
                if window_start <= date <= today:
                    recent.append(s)
            except ValueError:
                pass
        return recent
    except Exception as e:
        print(f"   Erro ao buscar shows de {artist_name}: {e}")
        return []


def get_recent_setlists(artist_mbid=None, artist_name=None, n=SETLISTS_TO_ANALYZE):
    """Pega os N setlists mais recentes de um artista."""
    try:
        params = {"p": 1}
        if artist_mbid:
            url = f"https://api.setlist.fm/rest/1.0/artist/{artist_mbid}/setlists"
        else:
            url = "https://api.setlist.fm/rest/1.0/search/setlists"
            params["artistName"] = artist_name

        r = _setlistfm_session.get(
            url,
            headers={"x-api-key": SETLISTFM_API_KEY, "Accept": "application/json",
                     "Connection": "close"},
            params=params,
            timeout=(5, 10),
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data.get("setlist", [])[:n]
    except Exception as e:
        print(f"   Erro ao buscar setlists de {artist_name}: {e}")
        return []


def analyze_setlists(setlists):
    """
    Analisa N setlists e retorna músicas ranqueadas por frequência.
    Retorna lista de (song_name, count, percentage).
    """
    if not setlists:
        return []
    counter = Counter()
    total_shows = len(setlists)

    for setlist in setlists:
        songs_in_show = set()
        for section in setlist.get("sets", {}).get("set", []):
            for song in section.get("song", []):
                name = song.get("name", "").strip()
                if name:
                    songs_in_show.add(name)
        counter.update(songs_in_show)

    ranked = []
    for song, count in counter.most_common():
        pct = round((count / total_shows) * 100)
        ranked.append((song, count, pct))

    return ranked


# ── Spotify ───────────────────────────────────────────────────────────────────

def get_spotify_user_id():
    r = _spotify_session.get(
        "https://api.spotify.com/v1/me",
        headers=_spotify_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["id"]


def search_track(artist_name, track_name):
    """Busca uma faixa no Spotify e retorna o URI. Usa cache local."""
    cache_key = f"{artist_name}||{track_name}"
    if cache_key in _uri_cache:
        return _uri_cache[cache_key]

    query = f"track:{track_name} artist:{artist_name}"
    try:
        r = _spotify_session.get(
            "https://api.spotify.com/v1/search",
            headers=_spotify_headers(),
            params={"q": query, "type": "track", "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
    except (requests.exceptions.RetryError, requests.exceptions.HTTPError):
        return None
    items = r.json().get("tracks", {}).get("items", [])
    uri = items[0]["uri"] if items else None

    _uri_cache[cache_key] = uri
    _save_uri_cache()
    return uri


def create_playlist(artist_name, track_uris):
    """Cria playlist no Spotify e adiciona as faixas."""
    playlist_name = f"{artist_name} Tour Setlist {datetime.now().year}"
    description = "Predicted setlist based on the latest shows."

    r = _spotify_session.post(
        "https://api.spotify.com/v1/me/playlists",
        headers={**_spotify_headers(), "Content-Type": "application/json"},
        json={"name": playlist_name, "description": description, "public": True},
        timeout=15,
    )
    r.raise_for_status()
    playlist_id  = r.json()["id"]
    playlist_url = r.json()["external_urls"]["spotify"]

    for i in range(0, len(track_uris), 100):
        batch = track_uris[i:i + 100]
        r = _spotify_session.post(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers={**_spotify_headers(), "Content-Type": "application/json"},
            json={"uris": batch},
            timeout=15,
        )
        r.raise_for_status()

    return playlist_url


# ── Fluxo principal ───────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("  Concert Tracker — São Paulo")
    print("=" * 60)

    _load_uri_cache()

    print("\nAutenticando no Spotify...")
    _get_spotify_token()
    user_id = get_spotify_user_id()
    print(f"   Conectado como {user_id}")

    top_artists = get_top_artists()

    # ── Scraping dos sites de ingressos ──────────────────────────────────────
    print("\nBuscando shows anunciados em São Paulo...")

    print("   Ticketmaster... ", end="", flush=True)
    tm_texts = _fetch_ticketmaster_sp()
    print(f"{len(tm_texts)} elementos")

    print("   30e... ", end="", flush=True)
    thirty_e_texts = _fetch_30e_sp()
    print(f"{len(thirty_e_texts)} elementos")

    print("   Eventim (Playwright)... ", end="", flush=True)
    eventim_texts = _fetch_eventim_sp()
    print(f"{len(eventim_texts)} elementos")

    # ── Match artistas × shows ────────────────────────────────────────────────
    print(f"\nCruzando {len(top_artists)} artistas com shows anunciados...")
    print("   (artistas não encontrados nos sites serão buscados no Setlist.fm)\n")

    matches = []

    for i, artist in enumerate(top_artists, 1):
        sys.stdout.write(f"\r   Verificando {i}/{len(top_artists)}: {artist[:40]:<40}")
        sys.stdout.flush()

        sources_found = []
        if _artist_in_texts(artist, tm_texts):
            sources_found.append("Ticketmaster")
        if _artist_in_texts(artist, thirty_e_texts):
            sources_found.append("30e")
        if _artist_in_texts(artist, eventim_texts):
            sources_found.append("Eventim")

        if sources_found:
            matches.append({"artist": artist, "shows": [{"source": sources_found}]})
        else:
            shows = search_upcoming_shows_sp(artist)
            if shows:
                matches.append({"artist": artist, "shows": shows})
            time.sleep(2.0)

    print(f"\n\n{len(matches)} match(es) encontrado(s)!\n")

    if MAX_PLAYLISTS is not None:
        matches = matches[:MAX_PLAYLISTS]
        print(f"   (limitado a {MAX_PLAYLISTS} playlists para este teste)\n")

    if not matches:
        print("Nenhum show encontrado para seus artistas favoritos em SP no momento.")
        return

    print("Aguardando 10s antes de iniciar buscas no Spotify...\n")
    time.sleep(10)

    # ── Playlists ─────────────────────────────────────────────────────────────
    for match in matches:
        artist = match["artist"]
        shows  = match["shows"]

        print(f"{'─' * 60}")
        print(f"{artist}")

        sources = shows[0].get("source")
        if sources:
            print(f"   anunciado em: {', '.join(sources)}")
            mbid = None
        else:
            print(f"   {len(shows)} show(s) registrado(s) no Setlist.fm (últimos 12 meses)")
            mbid = shows[0].get("artist", {}).get("mbid")

        print(f"   Analisando últimos {SETLISTS_TO_ANALYZE} setlists...")
        recent_setlists = get_recent_setlists(artist_mbid=mbid, artist_name=artist)

        if not recent_setlists:
            print("   Sem setlists suficientes, pulando.")
            continue

        ranked_songs = analyze_setlists(recent_setlists)
        filtered = [(s, c, p) for s, c, p in ranked_songs if c >= MIN_SONG_APPEARANCES]

        if not filtered:
            print("   Nenhuma música com aparições suficientes, pulando.")
            continue

        print(f"\n   Top músicas mais prováveis (de {len(recent_setlists)} shows):")
        for song, count, pct in filtered[:15]:
            bar = "█" * (pct // 10)
            print(f"      {pct:3d}% {bar:<10} {song}")

        print(f"\n   Buscando músicas no Spotify...")
        track_uris = []
        not_found  = []

        for song, count, pct in filtered:
            uri = search_track(artist, song)
            if uri:
                track_uris.append(uri)
            else:
                not_found.append(song)
            time.sleep(0.3)

        print(f"   {len(track_uris)} faixas encontradas no Spotify")
        if not_found:
            print(f"   Não encontradas: {', '.join(not_found[:5])}" +
                  (f" (+{len(not_found)-5})" if len(not_found) > 5 else ""))

        if not track_uris:
            print("   Nenhuma faixa encontrada, pulando playlist.")
            continue

        print(f"   Criando playlist no Spotify...")
        playlist_url = create_playlist(artist, track_uris)
        print(f"   Playlist criada: {playlist_url}\n")

    print("=" * 60)
    print("  Concluído!")
    print("=" * 60)


if __name__ == "__main__":
    run()
