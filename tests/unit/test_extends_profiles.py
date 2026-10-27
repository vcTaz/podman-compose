# SPDX-License-Identifier: GPL-2.0
# pylint: disable=protected-access
from __future__ import annotations

import argparse
import os
import tempfile
import unittest

import yaml

from podman_compose import PodmanCompose

COMPOSE_YAML = """
services:
    base:
        image: busybox
        profiles: [inherited]
    child:
        extends: base
    other:
        image: busybox
"""

CHAIN_YAML = """
services:
    base:
        image: busybox
        profiles: [inherited]
    child:
        extends: base
        profiles: [own]
    grand:
        extends: child
"""


class TestExtendsWithProfiles(unittest.TestCase):
    def parse(
        self, compose_yaml: str, profiles: list[str], services: list[str] | None = None
    ) -> PodmanCompose:
        with tempfile.TemporaryDirectory() as tmp_dir:
            compose_file = os.path.join(tmp_dir, "docker-compose.yaml")
            with open(compose_file, "w", encoding="utf-8") as f:
                f.write(compose_yaml)

            podman_compose = PodmanCompose()
            podman_compose.global_args = argparse.Namespace(
                file=[compose_file],
                project_name="test_proj",
                env_file=None,
                profile=profiles,
                in_pod="false",
                pod_args=None,
                no_normalize=False,
                services=services,
            )
            podman_compose._parse_compose_file()
            return podman_compose

    def test_base_profile_inactive(self) -> None:
        # the child inherits the base's profile, so both are inactive (as in Docker Compose)
        podman_compose = self.parse(COMPOSE_YAML, [])
        self.assertEqual(set(podman_compose.services), {"other"})
        merged = yaml.safe_load(podman_compose.merged_yaml)
        self.assertEqual(set(merged["services"]), {"other"})

    def test_base_profile_active(self) -> None:
        podman_compose = self.parse(COMPOSE_YAML, ["inherited"])
        self.assertEqual(set(podman_compose.services), {"base", "child", "other"})
        child = podman_compose.services["child"]
        self.assertEqual(child["image"], "busybox")
        self.assertEqual(child["profiles"], ["inherited"])
        # extends is not a runtime dependency
        self.assertEqual(podman_compose.services["child"]["_deps"], set())
        self.assertNotIn("_dependents", podman_compose.services["base"])

    def test_targeting_child_activates_inherited_profile(self) -> None:
        podman_compose = self.parse(COMPOSE_YAML, [], services=["child"])
        self.assertEqual(set(podman_compose.services), {"base", "child", "other"})

    def test_merged_yaml_has_resolved_child(self) -> None:
        podman_compose = self.parse(COMPOSE_YAML, ["inherited"])
        merged = yaml.safe_load(podman_compose.merged_yaml)
        child = merged["services"]["child"]
        self.assertEqual(child["image"], "busybox")
        self.assertEqual(child["profiles"], ["inherited"])
        self.assertNotIn("_deps", child)
        self.assertNotIn("_dependents", merged["services"]["base"])

    def test_chain_merges_profiles(self) -> None:
        podman_compose = self.parse(CHAIN_YAML, ["own"])
        self.assertEqual(set(podman_compose.services), {"child", "grand"})
        for name in ("child", "grand"):
            service = podman_compose.services[name]
            self.assertEqual(service["image"], "busybox")
            self.assertEqual(service["profiles"], ["inherited", "own"])

    def test_chain_no_profiles_active(self) -> None:
        podman_compose = self.parse(CHAIN_YAML, [])
        self.assertEqual(podman_compose.services, {})

    def test_missing_base_still_reported(self) -> None:
        compose_yaml = """
services:
    child:
        image: busybox
        extends: missing
"""
        with self.assertRaisesRegex(
            KeyError, "missing not found at services.child.extends definition"
        ):
            self.parse(compose_yaml, [])
