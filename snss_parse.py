#!/usr/bin/env python3
"""Parse a Chrome SNSS session/tabs file and extract window->tab->URL structure."""
import struct, sys, re, json

MAGIC = b'SNSS'

CMD_SET_TAB_WINDOW = 0
CMD_SET_WINDOW_BOUNDS = 1
CMD_SET_TAB_INDEX_IN_WINDOW = 2
CMD_TAB_CLOSED = 3
CMD_WINDOW_CLOSED = 4
CMD_TAB_NAV_PRUNED_BACK = 5
CMD_UPDATE_TAB_NAVIGATION = 6
CMD_SET_SELECTED_NAV_INDEX = 7
CMD_SET_SELECTED_TAB = 8
CMD_SET_WINDOW_TYPE = 9
CMD_SET_WINDOW_BOUNDS2 = 10
CMD_TAB_NAV_PRUNED_FRONT = 11
CMD_SET_PINNED_STATE = 12
CMD_SET_EXTENSION_APP_ID = 13
CMD_SET_WINDOW_APP_NAME = 14
CMD_SET_TAB_USER_AGENT = 15
CMD_SESSION_STORAGE_ASSOC = 16
CMD_SET_ACTIVE_WINDOW = 17
CMD_LAST_ACTIVE_TIME = 18
CMD_SET_WINDOW_WORKSPACE = 19
CMD_SET_WINDOW_WORKSPACE2 = 20

URL_RE = re.compile(rb'(https?://[\x21-\x7e]{4,4000}|chrome://[\x21-\x7e]{2,300}|file://[\x21-\x7e]{2,300})')


class PickleReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def remaining(self):
        return len(self.data) - self.pos

    def read_int(self):
        if self.remaining() < 4:
            raise EOFError
        v = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_int64(self):
        if self.remaining() < 8:
            raise EOFError
        v = struct.unpack_from('<q', self.data, self.pos)[0]
        self.pos += 8
        return v

    def read_bool(self):
        return bool(self.read_int())

    def _pad(self, n):
        # pickle aligns each write to 4-byte boundary
        rem = n % 4
        if rem:
            self.pos += (4 - rem)

    def read_string(self):
        length = self.read_int()
        if length < 0 or length > self.remaining():
            raise ValueError('bad string length %d' % length)
        s = self.data[self.pos:self.pos + length]
        self.pos += length
        self._pad(length)
        try:
            return s.decode('utf-8', errors='replace')
        except Exception:
            return s.decode('latin1', errors='replace')

    def read_string16(self):
        length = self.read_int()  # number of UTF-16 code units
        nbytes = length * 2
        if length < 0 or nbytes > self.remaining():
            raise ValueError('bad string16 length %d' % length)
        s = self.data[self.pos:self.pos + nbytes]
        self.pos += nbytes
        self._pad(nbytes)
        try:
            return s.decode('utf-16-le', errors='replace')
        except Exception:
            return ''


def iter_commands(data):
    pos = 4 + 4  # magic + version
    n = len(data)
    while pos + 2 <= n:
        size = struct.unpack_from('<H', data, pos)[0]
        pos += 2
        if size == 0:
            continue
        if pos + size > n:
            break
        cmd_id = data[pos]
        payload = data[pos + 1:pos + size]
        yield cmd_id, payload
        pos += size


def fallback_url_from_payload(payload):
    m = URL_RE.search(payload)
    if m:
        return m.group(0).decode('utf-8', errors='replace')
    return None


def parse_session_file(path):
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] != MAGIC:
        raise ValueError('not an SNSS file: %s' % path)

    tab_window = {}       # tab_id -> window_id
    tab_index_in_window = {}
    window_type = {}
    closed_tabs = set()
    closed_windows = set()
    active_window = None
    # tab_id -> {index: (url, title)}
    tab_navs = {}
    tab_selected_index = {}
    tab_pinned = set()
    window_last_active = {}

    for cmd_id, payload in iter_commands(data):
        try:
            if cmd_id == CMD_SET_TAB_WINDOW:
                r = PickleReader(payload)
                window_id = r.read_int()
                tab_id = r.read_int()
                tab_window[tab_id] = window_id
            elif cmd_id == CMD_SET_TAB_INDEX_IN_WINDOW:
                r = PickleReader(payload)
                tab_id = r.read_int()
                idx = r.read_int()
                tab_index_in_window[tab_id] = idx
            elif cmd_id == CMD_TAB_CLOSED:
                r = PickleReader(payload)
                tab_id = r.read_int()
                closed_tabs.add(tab_id)
            elif cmd_id == CMD_WINDOW_CLOSED:
                r = PickleReader(payload)
                window_id = r.read_int()
                closed_windows.add(window_id)
            elif cmd_id == CMD_UPDATE_TAB_NAVIGATION:
                r = PickleReader(payload)
                tab_id = r.read_int()
                index = r.read_int()
                url = None
                title = None
                try:
                    url = r.read_string()
                    title = r.read_string16()
                except Exception:
                    url = fallback_url_from_payload(payload)
                if not url:
                    url = fallback_url_from_payload(payload)
                if url:
                    tab_navs.setdefault(tab_id, {})[index] = (url, title)
                    # if this tab was previously marked closed via a stray event, unclose it
                    closed_tabs.discard(tab_id)
            elif cmd_id == CMD_SET_SELECTED_NAV_INDEX:
                r = PickleReader(payload)
                tab_id = r.read_int()
                idx = r.read_int()
                tab_selected_index[tab_id] = idx
            elif cmd_id == CMD_SET_WINDOW_TYPE:
                r = PickleReader(payload)
                window_id = r.read_int()
                wtype = r.read_int()
                window_type[window_id] = wtype
            elif cmd_id == CMD_SET_PINNED_STATE:
                r = PickleReader(payload)
                tab_id = r.read_int()
                pinned = r.read_bool()
                if pinned:
                    tab_pinned.add(tab_id)
            elif cmd_id == CMD_SET_ACTIVE_WINDOW:
                r = PickleReader(payload)
                active_window = r.read_int()
            elif cmd_id == CMD_LAST_ACTIVE_TIME:
                r = PickleReader(payload)
                window_id = r.read_int()
                ts = r.read_int64()
                window_last_active[window_id] = ts
        except (EOFError, ValueError, struct.error):
            # tolerate malformed / unexpected-shape commands, try regex fallback for nav cmd
            if cmd_id == CMD_UPDATE_TAB_NAVIGATION:
                url = fallback_url_from_payload(payload)
                if url:
                    # try to recover tab_id from first 4 bytes
                    try:
                        tab_id = struct.unpack_from('<i', payload, 0)[0]
                        tab_navs.setdefault(tab_id, {})[9999] = (url, None)
                    except Exception:
                        pass
            continue

    # Build final structure: window_id -> list of (tab_id, url, title, pinned)
    windows = {}
    for tab_id, navs in tab_navs.items():
        if tab_id in closed_tabs:
            continue
        window_id = tab_window.get(tab_id)
        # choose the selected/last navigation entry for this tab
        sel_idx = tab_selected_index.get(tab_id)
        if sel_idx is not None and sel_idx in navs:
            url, title = navs[sel_idx]
        else:
            # take highest index (most recent)
            last_idx = max(navs.keys())
            url, title = navs[last_idx]
        windows.setdefault(window_id, []).append({
            'tab_id': tab_id,
            'url': url,
            'title': title,
            'pinned': tab_id in tab_pinned,
            'order': tab_index_in_window.get(tab_id, 0),
        })

    result = []
    for window_id, tabs in windows.items():
        if window_id in closed_windows:
            continue
        tabs.sort(key=lambda t: t['order'])
        result.append({
            'window_id': window_id,
            'type': window_type.get(window_id),
            'last_active': window_last_active.get(window_id),
            'tabs': tabs,
        })
    result.sort(key=lambda w: (w['last_active'] or 0), reverse=True)
    return {'active_window': active_window, 'windows': result}


if __name__ == '__main__':
    paths = sys.argv[1:]
    merged_windows = {}
    for p in paths:
        try:
            r = parse_session_file(p)
        except Exception as e:
            print('ERR', p, e, file=sys.stderr)
            continue
        for w in r['windows']:
            wid = w['window_id']
            if wid not in merged_windows or len(w['tabs']) > len(merged_windows[wid]['tabs']):
                merged_windows[wid] = w
    out = {'windows': list(merged_windows.values())}
    print(json.dumps(out, indent=2, ensure_ascii=False))
