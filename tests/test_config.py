import os
import unittest
from unittest import mock
from pathlib import Path

from mml_sync import config


class TestConfig(unittest.TestCase):
    def test_schema_version_matches_typescript(self):
        self.assertEqual(config.SYNC_SCHEMA_VERSION, 1)

    def test_default_base_url_uses_https(self):
        self.assertTrue(config.DEFAULT_API_BASE_URL.startswith('https://'))

    def test_user_data_dir_is_per_user_and_under_home(self):
        with mock.patch.dict(os.environ, {'HOME': '/tmp/fake-home'}, clear=False):
            p = config.user_data_dir(home_override='/tmp/fake-home')
            self.assertEqual(p, Path('/tmp/fake-home/.mmlone/resolve-sync'))

    def test_project_dir_isolates_per_project(self):
        root = Path('/tmp/fake-home/.mmlone/resolve-sync')
        a = config.project_dir(root, 'projA')
        b = config.project_dir(root, 'projB')
        self.assertNotEqual(a, b)
        self.assertTrue(str(a).endswith('projects/projA'))


if __name__ == '__main__':
    unittest.main()
