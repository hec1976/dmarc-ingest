#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# High-Performance DMARC Parser & Forwarder
#
# IMAP -> DMARC XML (.xml/.gz/.zip/.tgz/.tar.gz) -> Syslog + JSON (daily) + optional Textlog
#
# Funktionen:
#  - IMAP Modus (Standard) mit BODY.PEEK[] (verhindert Datenverlust bei Absturz)
#  - File Modus (--file)
#  - Streaming zu Syslog (Splunk ready, inkl. TCP-Newline-Fix)
#  - Optimierter RAM-Cache zur Duplikat-Erkennung (Hoster-tauglich für 1000+ Domains)
#  - Schutz gegen XML- und Decompression-Bomben (ZIP/GZIP/TAR-Größenlimit)
#  - Trennung von Datei-Archivierung und Cache-Bereinigung
#  - Lockfile, damit nur ein Job gleichzeitig läuft

import imaplib
import email
try:
    from email import policy
    _EMAIL_POLICY = policy.default
except Exception:
    _EMAIL_POLICY = None

try:
    from email.header import decode_header, make_header
except Exception:
    decode_header = None
    make_header = None

import gzip
import zipfile
import tarfile
import io
import logging
import logging.handlers
import configparser
import os
import socket
import argparse
import json
import time
from datetime import datetime
import sys
import fcntl
import atexit

# XML / defusedxml
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring, ParseError as _XMLParseError
    _USING_DEFUSEDXML = True
except ImportError:
    from xml.etree.ElementTree import fromstring as _xml_fromstring, ParseError as _XMLParseError
    _USING_DEFUSEDXML = False


# ======================================================================
# Metadaten & Konstanten
# ======================================================================
__version__ = "1.2.2"
__author__ = "Claude Heinzmann"
__license__ = "MIT"

# Schutz gegen ZIP/GZIP-Bomben: Max. 50 MB pro entpackter XML-Datei im RAM
MAX_XML_SIZE = 50 * 1024 * 1024


# ======================================================================
# Argumente
# ======================================================================

parser = argparse.ArgumentParser(
    description='DMARC Parser: verarbeitet Reports via IMAP oder lokale Datei'
)
parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
parser.add_argument('--loglevel', default='INFO', help='Logging Level (z. B. INFO, DEBUG)')
parser.add_argument('--file', help='Lokale Datei zur Verarbeitung (.xml, .zip, .gz, .tgz, .tar.gz)')
args = parser.parse_args()

_valid_levels = {
    'CRITICAL': logging.CRITICAL,
    'ERROR': logging.ERROR,
    'WARNING': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
}
_loglevel_upper = str(args.loglevel or "INFO").upper()
LOGLEVEL = _valid_levels.get(_loglevel_upper, logging.INFO)


# ======================================================================
# Konfiguration einlesen (cron-sicher, relativ zum Script)
# ======================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

config = configparser.ConfigParser()
_read_files = config.read(CONFIG_PATH)

if not _read_files:
    print("Konnte Konfigurationsdatei nicht laden: {}".format(CONFIG_PATH), file=sys.stderr)
    sys.exit(1)

required_sections = ['options']
if not args.file:
    required_sections.append('imap')

for section in required_sections:
    if not config.has_section(section):
        print(
            "Fehlende Konfigurationssektion: [{}]. Bitte config.ini prüfen (Datei: {}).".format(
                section, CONFIG_PATH
            ),
            file=sys.stderr
        )
        sys.exit(1)

# IMAP Konfig nur lesen, wenn nötig
if config.has_section('imap'):
    IMAP_HOST = config.get("imap", "host", fallback=None)
    IMAP_USER = config.get("imap", "user", fallback=None)
    IMAP_PASS = config.get("imap", "password", fallback=None)
    IMAP_FOLDER = config.get("imap", "folder", fallback="INBOX")
    IMAP_ARCHIVE = config.get("imap", "archive_folder", fallback=None)
else:
    IMAP_HOST = IMAP_USER = IMAP_PASS = IMAP_FOLDER = IMAP_ARCHIVE = None

SYSLOG_ENABLED = config.getboolean("syslog", "enable", fallback=True)
SYSLOG_HOST = config.get("syslog", "host", fallback="localhost")
SYSLOG_PORT = config.getint("syslog", "port", fallback=514)
SYSLOG_PROTO = config.get("syslog", "protocol", fallback="tcp").lower()

SAVE_JSON = config.getboolean("options", "save_json", fallback=True)
XML_OUTPUT_DIR = config.get("options", "xml_output_dir", fallback="/tmp/dmarc_xml")
DAYS_TO_KEEP = config.getint("options", "days_to_keep", fallback=7)
CACHE_DAYS_TO_KEEP = config.getint("options", "processed_days_to_keep", fallback=DAYS_TO_KEEP)
DRY_RUN = config.getboolean("options", "dry_run", fallback=False)
LOG_RECORDS = config.getboolean("options", "log_records", fallback=True)
WRITE_TEXT_LOG = config.getboolean("options", "write_text_log", fallback=False)
TEXT_LOG_PATH = config.get("options", "text_log_path", fallback="dmarc_data.log")
# Standard auf ndjson für High-Performance, sofern in config.ini nicht anders angegeben
JSON_MODE = config.get("options", "json_mode", fallback="ndjson").lower()

PROCESSED_DB = config.get("options", "processed_db", fallback="/var/lib/dmarc-collector/processed.json")
LOCK_FILE = config.get("options", "lock_file", fallback="/run/lock/dmarc-collector.lock")

CONSOLE_LOG = config.getboolean("logging", "console", fallback=True)


# ======================================================================
# Logging
# ======================================================================

logger_script = logging.getLogger("DMARC_SCRIPT")
logger_script.setLevel(LOGLEVEL)

logger_data = logging.getLogger("DMARC_DATA")
logger_data.setLevel(logging.INFO)

formatter_script = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
formatter_syslog = logging.Formatter('DMARC: %(message)s')

if CONSOLE_LOG:
    sh = logging.StreamHandler()
    sh.setFormatter(formatter_script)
    sh.setLevel(LOGLEVEL)
    logger_script.addHandler(sh)

if config.getboolean("logging", "enable_file", fallback=False):
    file_path = config.get("logging", "file_path", fallback="dmarc-collector.log")
    try:
        base_dir = os.path.dirname(file_path) or "."
        os.makedirs(base_dir, exist_ok=True)
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setFormatter(formatter_script)
        file_handler.setLevel(LOGLEVEL)
        logger_script.addHandler(file_handler)
    except Exception as e:
        logger_script.error("[LOGGING] Konnte Log-Datei nicht öffnen ({}): {}".format(file_path, e))

if SYSLOG_ENABLED and not DRY_RUN:
    try:
        socktype = socket.SOCK_STREAM if SYSLOG_PROTO == "tcp" else socket.SOCK_DGRAM
        try:
            syslog_handler = logging.handlers.SysLogHandler(
                address=(SYSLOG_HOST, SYSLOG_PORT),
                socktype=socktype
            )
        except TypeError:
            syslog_handler = logging.handlers.SysLogHandler(
                address=(SYSLOG_HOST, SYSLOG_PORT)
            )
        
        # WICHTIG: Bei TCP-Syslog zwingend Zeilenumbruch als Trennzeichen setzen
        if SYSLOG_PROTO == "tcp":
            syslog_handler.terminator = "\n"
            
        syslog_handler.setFormatter(formatter_syslog)
        logger_data.addHandler(syslog_handler)
    except Exception as e:
        logger_script.error("[DMARC] Fehler beim Einrichten des Syslog Handlers: {}".format(e))

logger_script.info("Konfiguration geladen aus: {}".format(CONFIG_PATH))
if not _USING_DEFUSEDXML:
    logger_script.warning("[SECURITY] 'defusedxml' ist nicht installiert! Falle zurück auf Standard-XML-Parser.")


# ======================================================================
# Hilfsfunktionen
# ======================================================================

def _safe_text(x, default=""):
    return x if x is not None else default


def _decode_mime_header(raw):
    if not raw:
        return ""
    try:
        if decode_header and make_header:
            return str(make_header(decode_header(raw)))
        return raw
    except Exception:
        return raw


def safe_select(mail, folder):
    typ, _ = mail.select(folder)
    if typ != 'OK':
        logger_script.info("[IMAP] Ordner '{}' nicht vorhanden, versuche anzulegen.".format(folder))
        create_typ, _ = mail.create(folder)
        if create_typ != 'OK':
            raise RuntimeError("Konnte IMAP Ordner '{}' nicht anlegen.".format(folder))
        typ, _ = mail.select(folder)
        if typ != 'OK':
            raise RuntimeError("Konnte IMAP Ordner '{}' nicht selektieren.".format(folder))


def ensure_folder_exists(mail, folder, base_folder):
    if not folder:
        return
    typ, _ = mail.select(folder)
    if typ != 'OK':
        create_typ, _ = mail.create(folder)
        if create_typ != 'OK':
            raise RuntimeError("Konnte IMAP Ordner '{}' nicht anlegen.".format(folder))
    mail.select(base_folder)


def write_record_to_file(record):
    try:
        dir_name = os.path.dirname(TEXT_LOG_PATH)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        line = (
            "{} | org={} | report_id={} | domain={} | policy={} | ip={} | count={} | "
            "disposition={} | dkim={} | spf={} | header_from={} | dkim_domain={} | dkim_result={} | "
            "spf_domain={} | spf_result={}"
        ).format(
            datetime.now().isoformat(),
            _safe_text(record.get('org')),
            _safe_text(record.get('report_id')),
            _safe_text(record.get('domain')),
            _safe_text(record.get('policy')),
            _safe_text(record.get('ip')),
            _safe_text(record.get('count')),
            _safe_text(record.get('disposition')),
            _safe_text(record.get('dkim')),
            _safe_text(record.get('spf')),
            _safe_text(record.get('header_from')),
            _safe_text(record.get('dkim_domain')),
            _safe_text(record.get('dkim_result')),
            _safe_text(record.get('spf_domain')),
            _safe_text(record.get('spf_result')),
        )
        with open(TEXT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger_script.error("[DMARC] Fehler beim Schreiben der Datei-Ausgabe: {}".format(e))


def move_message(mail, msg_id, target_folder):
    if not target_folder:
        return False
    if isinstance(msg_id, bytes):
        msg_id = msg_id.decode('ascii', errors='ignore')
    else:
        msg_id = str(msg_id)
        
    typ, _ = mail.copy(msg_id, target_folder)
    if typ == 'OK':
        mail.store(msg_id, '+FLAGS', '\\Deleted')
        return True
    else:
        logger_script.warning("[IMAP] COPY nach '{}' fehlgeschlagen für ID {}.".format(target_folder, msg_id))
        return False


def _strip_namespaces(root):
    try:
        for elem in root.iter():
            tag = getattr(elem, "tag", None)
            if isinstance(tag, str) and '}' in tag:
                elem.tag = tag.split('}', 1)[1]
    except Exception:
        pass
    return root


def parse_dmarc_report(xml_data):
    results = []
    try:
        xml_data = xml_data.lstrip('\ufeff \t\r\n')
        root = _xml_fromstring(xml_data)
        root = _strip_namespaces(root)

        org = _safe_text(root.findtext("report_metadata/org_name"))
        report_id = _safe_text(root.findtext("report_metadata/report_id"))
        begin = _safe_text(root.findtext("report_metadata/date_range/begin"))
        end = _safe_text(root.findtext("report_metadata/date_range/end"))
        domain = _safe_text(root.findtext("policy_published/domain"))
        policy = _safe_text(root.findtext("policy_published/p"))

        for record in root.findall("record"):
            result = {
                "org": org,
                "report_id": report_id,
                "domain": domain,
                "policy": policy,
                "begin": begin,
                "end": end,
                "ip": _safe_text(record.findtext("row/source_ip")),
                "count": _safe_text(record.findtext("row/count")),
                "disposition": _safe_text(record.findtext("row/policy_evaluated/disposition")),
                "dkim": _safe_text(record.findtext("row/policy_evaluated/dkim")),
                "spf": _safe_text(record.findtext("row/policy_evaluated/spf")),
                "header_from": _safe_text(record.findtext("identifiers/header_from")),
                "dkim_domain": _safe_text(record.findtext("auth_results/dkim/domain")),
                "dkim_result": _safe_text(record.findtext("auth_results/dkim/result")),
                "spf_domain": _safe_text(record.findtext("auth_results/spf/domain")),
                "spf_result": _safe_text(record.findtext("auth_results/spf/result"))
            }
            results.append(result)
        return results

    except _XMLParseError as e:
        logger_script.error("[DMARC] Ungültiges XML Format: {}".format(e))
    except Exception as e:
        logger_script.exception("[DMARC] Fehler beim Parsen von XML: {}".format(e))
    return []


def log_record_to_syslog(record):
    log_line = (
        "org={} report_id={} domain={} policy={} ip={} count={} disposition={} dkim={} "
        "spf={} header_from={} begin={} end={} dkim_domain={} dkim_result={} spf_domain={} spf_result={}"
    ).format(
        _safe_text(record.get('org')),
        _safe_text(record.get('report_id')),
        _safe_text(record.get('domain')),
        _safe_text(record.get('policy')),
        _safe_text(record.get('ip')),
        _safe_text(record.get('count')),
        _safe_text(record.get('disposition')),
        _safe_text(record.get('dkim')),
        _safe_text(record.get('spf')),
        _safe_text(record.get('header_from')),
        _safe_text(record.get('begin')),
        _safe_text(record.get('end')),
        _safe_text(record.get('dkim_domain')),
        _safe_text(record.get('dkim_result')),
        _safe_text(record.get('spf_domain')),
        _safe_text(record.get('spf_result')),
    )
    logger_data.info(log_line)


def is_supported_archive(filename):
    fn = (filename or "").lower()
    return any(fn.endswith(ext) for ext in [".gz", ".zip", ".tgz", ".tar.gz"])


def extract_xml_from_archive(filename, payload):
    xml_list = []
    try:
        fn = (filename or "").lower()
        if fn.endswith(".gz") and not fn.endswith(".tar.gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gz:
                data = gz.read(MAX_XML_SIZE + 1)
                if len(data) > MAX_XML_SIZE:
                    raise ValueError("GZIP-Inhalt überschreitet Maximalgröße von {} MB".format(MAX_XML_SIZE // (1024 * 1024)))
                xml_list.append(data.decode("utf-8", errors="replace"))
        elif fn.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for zipinfo in zf.infolist():
                    if zipinfo.filename.lower().endswith(".xml"):
                        if zipinfo.file_size > MAX_XML_SIZE:
                            logger_script.warning("[DMARC] XML in ZIP zu groß ({}): übersprungen.".format(zipinfo.filename))
                            continue
                        with zf.open(zipinfo) as file:
                            data = file.read(MAX_XML_SIZE + 1)
                            if len(data) > MAX_XML_SIZE:
                                logger_script.warning("[DMARC] XML in ZIP überschreitet Limit beim Lesen ({}): übersprungen.".format(zipinfo.filename))
                                continue
                            xml_list.append(data.decode("utf-8", errors="replace"))
        elif fn.endswith(".tar.gz") or fn.endswith(".tgz"):
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile() and member.name.lower().endswith(".xml"):
                        if member.size > MAX_XML_SIZE:
                            logger_script.warning("[DMARC] XML in TAR zu groß ({}): übersprungen.".format(member.name))
                            continue
                        f = tar.extractfile(member)
                        if f:
                            data = f.read(MAX_XML_SIZE + 1)
                            if len(data) > MAX_XML_SIZE:
                                logger_script.warning("[DMARC] XML in TAR überschreitet Limit beim Lesen ({}): übersprungen.".format(member.name))
                                continue
                            xml_list.append(data.decode("utf-8", errors="replace"))
    except Exception as e:
        logger_script.error("[DMARC] Fehler beim Entpacken von {}: {}".format(filename, e))
    return xml_list


def load_existing_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger_script.error("[DMARC] JSON Datei beschädigt oder ungültig ({}). Starte neu: {}".format(e, path))
        return []
    except Exception as e:
        logger_script.error("[DMARC] Fehler beim Lesen von {}: {}".format(path, e))
        return []


def write_json(path, data):
    base = os.path.dirname(path) or "."
    os.makedirs(base, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def append_to_daily_json(records):
    if not records:
        return
    try:
        os.makedirs(XML_OUTPUT_DIR, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        filename = "{}_all.json".format(date_str)
        path = os.path.join(XML_OUTPUT_DIR, filename)

        if JSON_MODE == "ndjson":
            with open(path, "a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logger_script.info("[DMARC] Tages JSON (NDJSON) aktualisiert: {} (+{} Records)".format(path, len(records)))
            return

        data = load_existing_json(path)
        data.extend(records)
        write_json(path, data)
        logger_script.info("[DMARC] Tages JSON aktualisiert: {} (+{} Records)".format(path, len(records)))
    except Exception as e:
        logger_script.exception("[DMARC] Fehler beim Schreiben der JSON Datei: {}".format(e))


def cleanup_old_reports():
    try:
        if not os.path.isdir(XML_OUTPUT_DIR):
            return
        now = time.time()
        cutoff = now - DAYS_TO_KEEP * 86400
        for fname in os.listdir(XML_OUTPUT_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(XML_OUTPUT_DIR, fname)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logger_script.info("[DMARC] Alte Datei gelöscht: {}".format(fname))
            except Exception as e:
                logger_script.error("[DMARC] Fehler beim Löschen von {}: {}".format(fname, e))
    except Exception as e:
        logger_script.error("[DMARC] Fehler beim Aufräumen alter Reports: {}".format(e))


def _collect_xmls_from_message(msg):
    xml_texts = []
    for part in msg.walk():
        filename = part.get_filename()
        ctype = (part.get_content_type() or "").lower()
        if not filename and ("zip" in ctype or "gzip" in ctype or "xml" in ctype or "tar" in ctype):
            if "zip" in ctype:
                ext = "zip"
            elif "tar" in ctype:
                ext = "tar.gz"
            elif "gzip" in ctype:
                ext = "gz"
            else:
                ext = "xml"
            filename = "part." + ext
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        fn_low = filename.lower()
        if is_supported_archive(fn_low):
            xml_texts.extend(extract_xml_from_archive(fn_low, payload))
        elif fn_low.endswith(".xml"):
            try:
                if len(payload) > MAX_XML_SIZE:
                    logger_script.warning("[DMARC] XML-Anhang zu groß ({}): übersprungen.".format(filename))
                    continue
                xml_texts.append(payload.decode("utf-8", errors="replace"))
            except Exception as e:
                logger_script.exception("[DMARC] Fehler beim Verarbeiten von {}: {}".format(filename, e))
    return xml_texts


# ======================================================================
# Duplikat Erkennung (Optimiert für High-Performance)
# ======================================================================

_processed_cache = set()


def purge_old_processed():
    """
    Bereinigt den RAM-Cache basierend auf CACHE_DAYS_TO_KEEP.
    Dies hält die processed_db klein und schnell.
    """
    global _processed_cache

    if not _processed_cache or CACHE_DAYS_TO_KEEP <= 0:
        return

    now_ts = int(time.time())
    cutoff_ts = now_ts - CACHE_DAYS_TO_KEEP * 86400

    keep = set()
    removed = 0

    for key in _processed_cache:
        try:
            parts = key.split("|")
            end_str = parts[3] if len(parts) >= 4 else ""
            end_ts = int(end_str)
        except Exception:
            # Beschädigte oder nicht-nummerische Keys behalten, um Re-Processing-Loops zu vermeiden
            keep.add(key)
            continue

        if end_ts >= cutoff_ts:
            keep.add(key)
        else:
            removed += 1

    if removed:
        logger_script.info(
            "[DMARC] {} alte Einträge aus Deduplizierungs-Cache entfernt (Cache-Limit: {} Tage).".format(
                removed, CACHE_DAYS_TO_KEEP
            )
        )

    _processed_cache = keep


def load_processed_cache():
    """
    Lädt die DB nur in den RAM. Kein Aufräumen hier -> Maximale Startgeschwindigkeit.
    """
    global _processed_cache
    if not PROCESSED_DB or not os.path.exists(PROCESSED_DB):
        _processed_cache = set()
        return
    try:
        with open(PROCESSED_DB, "r", encoding="utf-8") as f:
            items = json.load(f)
            _processed_cache = set(items)
        logger_script.debug("[DMARC] processed_db geladen: {} Einträge".format(len(_processed_cache)))
    except Exception as e:
        logger_script.error("[DMARC] Konnte processed_db nicht laden ({}), starte mit leerem Cache: {}".format(PROCESSED_DB, e))
        _processed_cache = set()


def save_processed_cache():
    """
    Räumt auf und speichert dann atomar. Hält die Datei auf der Festplatte klein.
    """
    if not PROCESSED_DB or DRY_RUN:
        return
    try:
        purge_old_processed()

        base = os.path.dirname(PROCESSED_DB) or "."
        os.makedirs(base, exist_ok=True)
        tmp = PROCESSED_DB + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(_processed_cache), f, indent=2, ensure_ascii=False)

        os.replace(tmp, PROCESSED_DB)
        logger_script.debug("[DMARC] processed_db gespeichert: {} Einträge".format(len(_processed_cache)))
    except Exception as e:
        logger_script.error("[DMARC] Konnte processed_db nicht speichern: {}".format(e))


def make_report_key(records):
    if not records:
        return None
    r = records[0]
    return "{}|{}|{}|{}".format(
        _safe_text(r.get("org")),
        _safe_text(r.get("report_id")),
        _safe_text(r.get("begin")),
        _safe_text(r.get("end")),
    )


def is_duplicate_report(records):
    if DRY_RUN:
        return False
    key = make_report_key(records)
    if not key:
        return False
    if key in _processed_cache:
        return True
    _processed_cache.add(key)
    return False


def emit_records(records):
    """
    Zentrale Ausgabefunktion für Syslog, Text-Log und Tages-JSON.
    Sauber getrennt, damit Text-Log auch ohne aktives Syslog funktioniert.
    """
    if not records or DRY_RUN:
        return

    if LOG_RECORDS:
        for r in records:
            if SYSLOG_ENABLED:
                log_record_to_syslog(r)
            if WRITE_TEXT_LOG:
                write_record_to_file(r)

    if SAVE_JSON:
        append_to_daily_json(records)


# ======================================================================
# Lockfile
# ======================================================================

_lock_fh = None


def acquire_lock():
    global _lock_fh

    if not LOCK_FILE:
        return

    try:
        base = os.path.dirname(LOCK_FILE) or "."
        os.makedirs(base, exist_ok=True)
    except Exception as e:
        logger_script.debug("[LOCK] Konnte Lock-Verzeichnis nicht prüfen/erstellen: {}".format(e))

    try:
        _lock_fh = open(LOCK_FILE, "w")
    except Exception as e:
        logger_script.error("[LOCK] Kann Lockfile nicht öffnen: {} ({})".format(LOCK_FILE, e))
        sys.exit(1)

    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger_script.error("[LOCK] Job läuft bereits (Lockfile: {}). Beende.".format(LOCK_FILE))
        sys.exit(1)
    except Exception as e:
        logger_script.error("[LOCK] Konnte Lock nicht setzen: {}".format(e))
        sys.exit(1)

    try:
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(str(os.getpid()) + "\n")
        _lock_fh.flush()
    except Exception:
        pass


def release_lock():
    global _lock_fh
    if not _lock_fh:
        return
    try:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
        except Exception:
            pass
        _lock_fh.close()
    except Exception:
        pass
    _lock_fh = None


atexit.register(release_lock)


# ======================================================================
# IMAP und File Modus
# ======================================================================

def connect_imap():
    if not IMAP_HOST or not IMAP_USER or not IMAP_PASS:
        logger_script.error("[IMAP] IMAP Konfiguration unvollständig.")
        sys.exit(1)
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASS)
        return mail
    except Exception as e:
        logger_script.error("[IMAP] Login fehlgeschlagen: {}".format(e))
        sys.exit(1)


def process_dmarc_reports():
    mail = connect_imap()
    safe_select(mail, IMAP_FOLDER)

    if IMAP_ARCHIVE and not DRY_RUN:
        try:
            ensure_folder_exists(mail, IMAP_ARCHIVE, IMAP_FOLDER)
        except Exception as e:
            logger_script.error("[IMAP] Konnte Archivordner nicht vorbereiten: {}".format(e))

    typ, data = mail.search(None, "UNSEEN")
    ids = (data[0].split() if typ == 'OK' and data and data[0] else [])
    logger_script.info("[IMAP] {} ungelesene DMARC Mails im Ordner '{}' gefunden.".format(len(ids), IMAP_FOLDER))

    for num in ids:
        try:
            # BODY.PEEK[] liest die Mail, ohne das \Seen-Flag sofort auf dem Server zu setzen
            typ, msg_data = mail.fetch(num, "(BODY.PEEK[])")
            if typ != 'OK' or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            if _EMAIL_POLICY:
                msg = email.message_from_bytes(raw_email, policy=_EMAIL_POLICY)
            else:
                msg = email.message_from_bytes(raw_email)

            subject = _decode_mime_header(msg.get('Subject'))
            from_hdr = _decode_mime_header(msg.get('From'))
            logger_script.debug("[IMAP] Bearbeite Message ID {!r} | From: {} | Subject: {}".format(num, from_hdr, subject))

            new_records = []
            has_valid_xml = False

            # Jedes XML im Anhang einzeln auf Duplikate prüfen
            for xml_text in _collect_xmls_from_message(msg):
                records = parse_dmarc_report(xml_text)
                if not records:
                    continue
                has_valid_xml = True
                if is_duplicate_report(records):
                    logger_script.info("[DMARC] Report '{}' bereits verarbeitet, wird übersprungen.".format(make_report_key(records)))
                    continue
                new_records.extend(records)

            if not has_valid_xml:
                logger_script.warning("[DMARC] Keine gültigen DMARC Daten in Message ID {!r} gefunden.".format(num))
                continue

            # Daten schreiben / senden
            emit_records(new_records)

            # Erst nach erfolgreicher Verarbeitung Mail verschieben oder als gelesen markieren
            if not DRY_RUN:
                if IMAP_ARCHIVE:
                    moved = move_message(mail, num, IMAP_ARCHIVE)
                    if not moved:
                        mail.store(num, '+FLAGS', '\\Seen')
                else:
                    mail.store(num, '+FLAGS', '\\Seen')

        except Exception as e:
            logger_script.exception("[DMARC] Fehler bei Mail ID {}: {}".format(num, e))

    try:
        mail.expunge()
    except Exception as e:
        logger_script.debug("[IMAP] Expunge fehlgeschlagen: {}".format(e))

    try:
        mail.logout()
    except Exception as e:
        logger_script.debug("[IMAP] Logout fehlgeschlagen: {}".format(e))

    if not DRY_RUN:
        cleanup_old_reports()
    logger_script.info("[DMARC] Verarbeitung abgeschlossen.")


def process_file_mode(filename):
    try:
        if not os.path.exists(filename):
            logger_script.error("[DMARC] Datei nicht gefunden: {}".format(filename))
            sys.exit(1)

        with open(filename, "rb") as f:
            payload = f.read()

        xml_list = []
        fn_low = filename.lower()

        if fn_low.endswith(".xml"):
            if len(payload) > MAX_XML_SIZE:
                logger_script.error("[DMARC] Datei zu groß ({}): abgebrochen.".format(filename))
                sys.exit(1)
            xml_list = [payload.decode("utf-8", errors="replace")]
        elif is_supported_archive(fn_low):
            xml_list = extract_xml_from_archive(fn_low, payload)
        else:
            logger_script.error("[DMARC] Nicht unterstütztes Dateiformat: {}".format(filename))
            sys.exit(1)

        new_records = []
        has_valid_xml = False

        for xml in xml_list:
            records = parse_dmarc_report(xml)
            if not records:
                continue
            has_valid_xml = True
            if is_duplicate_report(records):
                logger_script.info("[DMARC] Report '{}' aus {} bereits verarbeitet, übersprungen.".format(make_report_key(records), filename))
                continue
            new_records.extend(records)

        if not has_valid_xml:
            logger_script.warning("[DMARC] Keine gültigen DMARC Daten in {} gefunden.".format(filename))
            return

        if not new_records:
            return

        emit_records(new_records)
        logger_script.info("[DMARC] {} Datensätze aus {} verarbeitet.".format(len(new_records), filename))

    except Exception as e:
        logger_script.exception("[DMARC] Fehler beim Verarbeiten der Datei {}: {}".format(filename, e))


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    logger_script.info("Starte DMARC Collector v{} (Python {})".format(__version__, sys.version.split()[0]))

    acquire_lock()
    load_processed_cache()

    if args.file:
        process_file_mode(args.file)
    else:
        process_dmarc_reports()

    save_processed_cache()
