"""Runtime image checks: TEST_IMAGE=<tag> python -m unittest discover -s tests."""

import json
import os
import subprocess
import unittest


class ImageSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = os.environ.get("TEST_IMAGE")
        if not image:
            raise unittest.SkipTest("Set TEST_IMAGE to the Docker image to test")
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "python", image, "-c",
                "import importlib.metadata as m, importlib.util, json; "
                "print(json.dumps({'urllib3': m.version('urllib3'), "
                "'pip_present': importlib.util.find_spec('pip') is not None}))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.inventory = json.loads(result.stdout)

    def test_urllib3_includes_security_fixes(self):
        version = tuple(int(part) for part in self.inventory["urllib3"].split("."))
        self.assertGreaterEqual(version, (2, 8, 0))

    def test_build_only_pip_and_its_vendored_libraries_are_removed(self):
        self.assertFalse(self.inventory["pip_present"])


if __name__ == "__main__":
    unittest.main()
