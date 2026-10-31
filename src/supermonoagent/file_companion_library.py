"""Selected-window local skills on the companion's own host; no MCP or terminal input."""
import threading

from .mylib_assistant import SkillIndex
from .mylib_local import LocalCatalog, LocalLibrary, LocalLibraryError


class CompanionLibrary:
    def __init__(self, workspaces, state_dir):
        self.workspaces = workspaces
        self.state_dir = state_dir
        self._lock = threading.Lock()
        self._root = None
        self._library = None
        self._index = None
        self._closed = False

    def _service(self):
        root = self.workspaces.get_mylib()['mylib_root']
        with self._lock:
            if self._closed:
                raise LocalLibraryError('cancelled')
            if root != self._root:
                if self._library is not None:
                    self._library.close()
                self._root = root
                self._library = LocalLibrary(root, self.state_dir / 'mylib-local')
                self._index = SkillIndex(root)
            return root, self._library, self._index

    def action(self, action, data):
        if action == 'library.get' and not data:
            return self.workspaces.get_mylib()
        if action == 'library.set' and set(data) == {'mylib_root', 'expected_revision'}:
            with self._lock:
                result = self.workspaces.save_mylib(data['mylib_root'], expected_revision=data['expected_revision'])
            return result
        root, library, index = self._service()
        if action == 'library.status' and not data:
            return library.status()
        if action == 'library.recommend' and set(data) == {'context'}:
            index.reload()
            return {**index.recommend(data['context']), 'library': {'kind': 'local', 'root': root}}
        if action == 'library.read_skill' and set(data) == {'source'}:
            catalog = LocalCatalog(root)
            cards = catalog.list_skills(limit=100)['items']
            card = next((item for item in cards if item['path'] == data['source']), None)
            if card is None:
                raise LocalLibraryError('unknown_skill')
            return catalog.read_skill(card['id'], catalog.revision)
        if action == 'library.pull' and not data:
            result = library.pull()
        elif action == 'library.commit_preview' and not data:
            return library.commit_preview()
        elif action == 'library.commit' and set(data) == {'fingerprint', 'message'}:
            result = library.commit_and_push(data['fingerprint'], data['message'])
        elif action == 'library.run_evolution' and set(data) == {'text', 'target'}:
            return library.run_evolution(data)
        elif action == 'library.apply_evolution' and set(data) == {'suggestion_id'}:
            result = library.apply_evolution(data['suggestion_id'])
        else:
            raise LocalLibraryError('invalid_request')
        index.reload()
        return result

    def close(self):
        self._closed = True
        with self._lock:
            if self._library is not None:
                self._library.close()
