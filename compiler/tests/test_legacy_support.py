"""测试显式旧版上下文的隔离；本文件本身不得进入 LegacyTestCase。"""
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.language_policy import current_profile, language_profile


class LegacySupportTests(unittest.TestCase):
    def test_instance_lifecycle_selects_legacy_and_restores_core(self):
        observed = []

        class Sample(LegacyTestCase):
            def setUp(self):
                observed.append(("setUp", current_profile()))
                self.addCleanup(lambda: observed.append(("cleanup", current_profile())))

            def test_body(self):
                observed.append(("test", current_profile()))

            def tearDown(self):
                observed.append(("tearDown", current_profile()))

        with language_profile("core"):
            result = Sample("test_body").run()
            self.assertEqual(current_profile(), "core")
        assert result is not None
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(observed, [(name, "legacy") for name in ("setUp", "test", "tearDown", "cleanup")])

    def test_test_failure_still_restores_callers_profile(self):
        class Failing(LegacyTestCase):
            def test_body(self):
                self.assertEqual(current_profile(), "legacy")
                self.fail("intentional fixture failure")

        with language_profile("core"):
            result = Failing("test_body").run()
            self.assertEqual(current_profile(), "core")
        assert result is not None
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.testsRun, 1)

    def test_class_fixtures_preserve_subclass_and_super_dispatch(self):
        observed = []

        class Parent(LegacyTestCase):
            @classmethod
            def setUpClass(cls):
                observed.append(("parent setup", cls.__name__, current_profile()))

            @classmethod
            def tearDownClass(cls):
                observed.append(("parent teardown", cls.__name__, current_profile()))

            def test_body(self):
                observed.append(("test", type(self).__name__, current_profile()))

        class Child(Parent):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                observed.append(("child setup", cls.__name__, current_profile()))

            @classmethod
            def tearDownClass(cls):
                observed.append(("child teardown", cls.__name__, current_profile()))
                super().tearDownClass()

        with language_profile("core"):
            result = unittest.TestSuite([Child("test_body")]).run(unittest.TestResult())
            self.assertEqual(current_profile(), "core")
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(observed, [(name, "Child", "legacy") for name in (
            "parent setup", "child setup", "test", "child teardown", "parent teardown")])

    def test_class_setup_failure_restores_context(self):
        class FailedSetup(LegacyTestCase):
            @classmethod
            def setUpClass(cls):
                if current_profile() != "legacy":
                    raise AssertionError("class setup was not explicitly legacy")
                raise RuntimeError("intentional setup failure")

            def test_body(self):
                self.fail("a failed class fixture must prevent this test")

        with language_profile("core"):
            result = unittest.TestSuite([FailedSetup("test_body")]).run(unittest.TestResult())
            self.assertEqual(current_profile(), "core")
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.testsRun, 0)
        self.assertIn("intentional setup failure", result.errors[0][1])


if __name__ == "__main__":
    unittest.main()
