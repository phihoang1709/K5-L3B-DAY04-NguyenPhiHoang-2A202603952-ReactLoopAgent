import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from load_env import load_env


class LoadEnvTests(unittest.TestCase):
    def test_loads_exported_and_quoted_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / '.env'
            env_file.write_text(
                "export PROVIDER=openrouter\nMODEL='openai/gpt-4.1-mini'\n",
                encoding='utf-8',
            )
            with patch.dict(os.environ, {}, clear=True):
                load_env(env_file)
                self.assertEqual(os.environ['PROVIDER'], 'openrouter')
                self.assertEqual(os.environ['MODEL'], 'openai/gpt-4.1-mini')

    def test_does_not_override_shell_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / '.env'
            env_file.write_text('MODEL=file-model\n', encoding='utf-8')
            with patch.dict(os.environ, {'MODEL': 'shell-model'}, clear=True):
                load_env(env_file)
                self.assertEqual(os.environ['MODEL'], 'shell-model')


if __name__ == '__main__':
    unittest.main()
