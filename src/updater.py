import functools
import os
from pathlib import Path
import re

import shpyx
import tomlkit
import tomlkit.exceptions

"""Poetry configuration file name"""
POETRY_CONFIG_FILE_NAME = "pyproject.toml"

"""Sections in the Poetry configuration files where dependencies reside"""
SECTIONS = ("dependencies", "dev-dependencies")

def is_exact_version(version: str) -> bool:
    v = version.strip()
    # Matches "4.8.0", "==4.8.0", "4.13.0", etc (but NOT "^4.8.0" or ">=4.8.0")
    return bool(re.fullmatch(r"(==)?\d+(\.\d+){1,2}", v))

def _run_updater_in_path(path: str) -> None:
    """
    Run the updater in the specified path.
    """
    # Mapping from projects to their path dependencies.
    file_path_to_deps: dict[Path, list[Path]] = {}

    # First iteration - find all projects and create a mapping from projects to their path dependencies.
    for root, _dirs, files in os.walk(path):
        for name in files:
            # Skip non Poetry configuration files.
            if name != POETRY_CONFIG_FILE_NAME:
                continue

            # Get the contents of the configuration file.
            file_path = Path(root).joinpath(name)
            file_contents = Path(file_path).read_text()
            parsed_contents = tomlkit.parse(file_contents)

            # Get the poetry configuration, skipping if there is none.
            try:
                poetry_section = parsed_contents["tool"]["poetry"]
            except tomlkit.exceptions.NonExistentKey:
                continue

            # Build the mapping from projects to their path dependencies.
            file_path_to_deps[file_path.resolve()] = []
            pairs_to_check = list(poetry_section.items())
            while pairs_to_check:
                current_pair = pairs_to_check.pop()
                if current_pair[0] in SECTIONS:
                    current_section = current_pair[1]
                elif isinstance(current_pair[1], dict):
                    pairs_to_check.extend(current_pair[1].items())
                    continue
                else:
                    continue

                for details in current_section.values():
                    if isinstance(details, dict) and "path" in details:
                        file_path_to_deps[file_path.resolve()].append((Path(root) / details["path"] / name).resolve())

    # Order the projects based on interdependencies, where dependencies go first.
    def cmp(x: Path, y: Path) -> int:
        if x in file_path_to_deps[y]:
            # X is a dependency of Y, mark it as smaller, so it appears first in the list.
            return -1
        else:
            return 1

    file_paths_in_order = sorted(file_path_to_deps, key=functools.cmp_to_key(cmp))

    # Second iteration - make sure all projects have lock files.
    for file_path in file_paths_in_order:
        shpyx.run("poetry lock", exec_dir=file_path.parent)

    # Third iteration - run updates in order.
    for file_path in file_paths_in_order:
        # Get the contents of the configuration file.
        file_contents = Path(file_path).read_text()
        parsed_contents = tomlkit.parse(file_contents)
        poetry_section = parsed_contents["tool"]["poetry"]

        print(f"TOML contents of {file_path}: {parsed_contents}")

        # Construct the poetry command to get outdated packages.
        poetry_cmd = "poetry show -o --no-ansi"
        if group_section := parsed_contents["tool"]["poetry"].get("group"):
            poetry_cmd += f" --with {','.join(group_section.keys())}"

        # Get all the outdated packages.
        results = shpyx.run(poetry_cmd, exec_dir=file_path.parent)

        if not results.stdout:
            # Nothing to update.
            continue

        # Update the file contents, for each outdated package.
        for result in results.stdout.strip().split("\n"):
            # Remove the "(!)" decoration used to mark packages as non installed.
            formatted_result = result.replace(" (!) ", " ")

            # Get the package details.
            package_name, installed_version, new_version = formatted_result.split()[:3]

            # Update the package version in the file.
            pairs_to_check = list(poetry_section.items())
            while pairs_to_check:
                current_pair = pairs_to_check.pop()
                if current_pair[0] in SECTIONS:
                    current_section = current_pair[1]
                elif isinstance(current_pair[1], dict):
                    pairs_to_check.extend(current_pair[1].items())
                    continue
                else:
                    continue

                try:
                    original_package_name, package_details = next(
                        (name, details)
                        for name, details in current_section.items()
                        if name.lower() == package_name.lower()
                    )
                except StopIteration:
                    # The package is not in this section.
                    continue

                # --- Robust version handling for string, dict, or list (of dicts/strings) ---

                versions_to_check = []
                if isinstance(package_details, str):
                    versions_to_check = [package_details]
                elif isinstance(package_details, dict):
                    versions_to_check = [package_details.get("version")]
                elif isinstance(package_details, list):
                    # List of tables or strings (env markers)
                    versions_to_check = []
                    for item in package_details:
                        if isinstance(item, dict):
                            versions_to_check.append(item.get("version"))
                        elif isinstance(item, str):
                            versions_to_check.append(item)
                else:
                    print(f"Unknown dependency format for {package_name}, skipping.")
                    continue

                # If any version is pinned exactly, skip updating this dependency
                if any(v and is_exact_version(v) for v in versions_to_check):
                    print("Skipping locked package:", package_name)
                    continue

                print(f"Updating {package_name}: {installed_version} -> {new_version}")

                # Update all possible versions in the dependency spec
                if isinstance(package_details, str):
                    current_section[original_package_name] = new_version
                elif isinstance(package_details, dict):
                    current_section[original_package_name]["version"] = new_version
                elif isinstance(package_details, list):
                    for idx, item in enumerate(package_details):
                        if isinstance(item, dict) and "version" in item:
                            current_section[original_package_name][idx]["version"] = new_version
                        elif isinstance(item, str):
                            current_section[original_package_name][idx] = new_version

        # Write the updated configuration file.
        Path(file_path).write_text(parsed_contents.as_string())

        # Finally, regenerate the lock file again, with the new package versions.
        shpyx.run("poetry update --lock", exec_dir=file_path.parent)

def run_updater(paths: list[str]) -> None:
    for path in paths:
        _run_updater_in_path(path)
