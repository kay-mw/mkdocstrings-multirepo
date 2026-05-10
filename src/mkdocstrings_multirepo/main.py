import concurrent.futures
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.exceptions import PluginError
from mkdocs.plugins import BasePlugin, get_plugin_logger
from platformdirs import user_cache_dir

log = get_plugin_logger(__name__)


@dataclass
class CloneInformation:
    """Dataclass describing the information needed to perform a git clone."""

    name: str
    """The name of the directory to clone into."""

    url: str
    """The `.git` URL of the repo to clone."""

    ref: str
    """The ref of the repo to clone."""

    handler: str
    """
    The language handler to use for this repo. Must be one of the
    [supported mkdocstrings handlers](https://mkdocstrings.github.io/usage/handlers/).
    """

    hashed_dir: Path
    """
    A digested SHA256 hash representing the unique combination of the `url` and `ref`.
    This serves as the parent directory to the repo name.
    """


class _Repos(base.Config):
    """Repo configuration attributes."""

    name = c.Type(str)
    """
    The name of the repo. This parameter determines what name prefix to use when
    referencing files from this repo in your documentation.

    Example: 'textual'
    Reference in documentation: `textual.path.to.some.module`
    """

    url = c.Type(str)
    """
    The `.git` URL of the repo.

    Example: 'https://github.com/Textualize/textual.git'
    """

    ref = c.Type(str)
    """
    The ref of the repo to clone. The ref can be any reference that git can checkout,
    such as a branch or commit hash. Commit hashes are recommended, for reproducibility
    when deploying in CI.

    Example: '501372027f3abc75561881e3803efc34098dabe1'
    Example: 'main'
    """

    handler = c.Choice(
        ("c", "crystal", "github", "python", "matlab", "shell", "typescript", "vba"),
        default="python",
    )
    """
    The language handler to use for this repo. Must be one of the
    [supported mkdocstrings handlers](https://mkdocstrings.github.io/usage/handlers/).
    """


class MkdocstringsMultirepoConfig(base.Config):
    """mkdocstrings-multirepo configuration options."""

    repos = c.ListOfItems(c.SubConfig(_Repos))
    """
    A list of one or more repos, each containing the attributes `name`, `url`, `ref`
    and `handler`.

    Example:

        ```yml
        repos:
          - name: 'textual'
            url: 'https://github.com/Textualize/textual.git'
            ref: '501372027f3abc75561881e3803efc34098dabe1'
            handler: 'python'
        ```
    """
    cache_limit_multiplier = c.Type(int, default=2)
    """
    An integer representing the multiplier to apply to the cache limit (default: 2).

    To prevent fetching repos on every build, mkdocstrings-multirepo caches repos in
    your OS's cache directory (`~/.cache/mkdocstrings-multirepo` on Linux). To prevent
    this cache getting too large, mkdocstrings-multirepo automatically deletes unused
    directories when the number of unused directories exceeds the cache limit.

    The cache limit is defined relative to the number of repos in the configuration,
    multiplied by this multiplier.

    This is 2 by default, meaning that **unused repos** will only be pruned when the
    number of unused repos exceeds the number of **currently configured repos**
    multiplied by 2.
    """


class MkdocstringsMultirepoPlugin(BasePlugin[MkdocstringsMultirepoConfig]):
    """
    Clones the git repos defined in the MkdocstringsMultirepoConfig into the OS's
    cache directory, and adds their paths to mkdocstrings `paths` configuration option.
    """

    def on_config(self, config: MkDocsConfig) -> None:
        """
        Hooks into the mkdocs config loading process, to clone the git repos defined
        in the MkdocstringsMultirepoConfig into the OS's cache directory, and add
        their paths to mkdocstrings `paths` configuration option.
        """
        cache_dir = Path(user_cache_dir("mkdocstrings-multirepo"))
        repos = self.config.repos

        git_supports_revision = False
        git_major_version, git_minor_version = self.get_git_version()
        if (
            git_major_version == 2 and git_minor_version >= 49
        ) or git_major_version > 2:
            git_supports_revision = True

        plugins = config.plugins
        if "mkdocstrings" not in plugins:
            defined_plugins = list(plugins.keys())
            raise PluginError(
                "mkdocstrings-multirepo: Failed to find the key 'mkdocstrings' in "
                "your plugin config. Are you sure you have 'mkdocstrings' defined "
                "under `plugins` in your `mkdocs.yml`?\n"
                f"Found plugins: {defined_plugins}"
            )

        handlers = plugins["mkdocstrings"].config.setdefault("handlers", {})
        clone_information = self.build_clone_information(
            repos=repos, cache_dir=cache_dir
        )
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_clone = {
                executor.submit(
                    self.prepare_repo,
                    name=info.name,
                    url=info.url,
                    ref=info.ref,
                    hashed_dir=info.hashed_dir,
                    git_supports_revision=git_supports_revision,
                ): info
                for info in clone_information
            }
            for future in concurrent.futures.as_completed(future_to_clone):
                info = future_to_clone[future]
                try:
                    cloned = future.result()

                    language = handlers.setdefault(info.handler, {})
                    paths = language.setdefault("paths", ["."])
                    paths.append(str(info.hashed_dir))

                    if cloned:
                        log.info(f"Fetched '{info.url}' at ref '{info.ref}'")
                except subprocess.CalledProcessError as e:
                    raise PluginError(
                        "mkdocstrings-multirepo: Failed to fetch git URL "
                        f"'{info.url}' for ref '{info.ref}'. See Git output below:\n"
                        f"{e.stderr}"
                    )

        configured_repos = [info.hashed_dir for info in clone_information]
        cached_repos = os.listdir(cache_dir)
        len_configured = len(configured_repos)
        n_unused_in_cache = len(cached_repos) - len_configured
        cache_limit = len_configured * self.config.cache_limit_multiplier
        if n_unused_in_cache > cache_limit:
            log.info(
                f"Detected {n_unused_in_cache} unused repo(s) in the cache,"
                f" which exceeds the cache limit of {cache_limit}. Pruning..."
            )
            cached_repos = [Path(cache_dir.joinpath(repo)) for repo in cached_repos]
            self.prune_cache(
                configured_repos=configured_repos,
                cached_repos=cached_repos,
                cache_dir=cache_dir,
            )

    @staticmethod
    def get_git_version() -> tuple[int, int]:
        """
        Gets the Git version of the machine running mkdocs. This is necessary to
        identify whether the machine has a git version >= 2.49.

        Git version 2.49 added the `--revision` flag to `git clone`, which allows
        users to fetch a specific ref with one command. Detecting versions that support
        this flag therefore allows mkdocstrings-multirepo to use this superior
        approach if it's available.

        Returns:
            A `tuple[int, int]`, where the first item in the tuple is the major version,
            and the second item is the minor version.
        """
        version_string = subprocess.run(
            ["git", "--version"], check=True, capture_output=True, text=True
        )
        version_number = version_string.stdout.split(" ")[2]
        version_components = version_number.split(".")
        major_version = int(version_components[0])
        minor_version = int(version_components[1])

        return major_version, minor_version

    @staticmethod
    def build_clone_information(
        repos: list[_Repos], cache_dir: Path
    ) -> list[CloneInformation]:
        """
        Builds the information necessary to clone a git repo into the OS's cache
        directory (`CloneInformation`), for each repo in the list.

        Parameters:
            repos: A list of repos, as defined in the mkdocstrings-multirepo
                configuration.
            cache_dir: The `Path` to mkdocstrings-multirepo's cache directory.

        Returns:
            `list[CloneInformation]`.
        """
        clone_information: list[CloneInformation] = []
        for repo in repos:
            name = repo.name
            url = repo.url
            ref = repo.ref
            handler = repo.handler

            if "/" in name or "\\" in name:
                log.warning(
                    f"Repo name {name} contains a slash. Including slashes"
                    " in names is discouraged, as this may result in your name prefix"
                    " being two directories (`before_slash.after_slash.my.module`) if"
                    " there are characters after the slash."
                )

            hashed_name = hashlib.sha256((url + ref).encode()).hexdigest()
            hashed_dir = cache_dir.joinpath(hashed_name)

            clone_information.append(
                CloneInformation(name, url, ref, handler, hashed_dir)
            )

        return clone_information

    @staticmethod
    def subprocess_run_wrapper(args: list[str], cwd: Path) -> None:
        """
        Wrapper around subprocess.run, to allow for reduced repetition of paramaters
        common across git commands run in
        `MkdocstringsMultirepoPlugin.clone_git_repo`.

        Parameters:
            args: The list of arguments to pass to `subprocess.run`
            cwd: The directory to run the subprocess in.
        """
        subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def clone_git_repo(
        git_supports_revision: bool, url: str, ref: str, output_path: Path
    ) -> None:
        """
        Clones the git repo for the specified `url` and `ref` into the provided
        `output_path`.

        Clones the specified ref using the `--revision` flag, git versions (>=2.49)
        that support it, as this approach is far simpler. Otherwise, uses the more
        complex legacy approach of initialising an empty repo at `output_path`, adding
        the URL as a remote, fetching the specific ref, and checking out the `HEAD`.

        Parameters:
            git_supports_revision: A `bool` indicating whether the machine running
            mkdocs supports the `git clone` `--revision` flag.
            url: The `.git` URL of the repo to clone.
            ref: The ref to checkout in the target repo.
            output_path: The path to clone the repo into.
        """
        if git_supports_revision:
            subprocess.run(
                [
                    "git",
                    "clone",
                    url,
                    output_path,
                    "--depth=1",
                    f"--revision={ref}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return

        os.makedirs(output_path)
        MkdocstringsMultirepoPlugin.subprocess_run_wrapper(
            args=["git", "init"], cwd=output_path
        )
        MkdocstringsMultirepoPlugin.subprocess_run_wrapper(
            args=["git", "remote", "add", "origin", url], cwd=output_path
        )
        MkdocstringsMultirepoPlugin.subprocess_run_wrapper(
            args=["git", "fetch", "--depth", "1", "origin", ref],
            cwd=output_path,
        )
        MkdocstringsMultirepoPlugin.subprocess_run_wrapper(
            args=["git", "checkout", "FETCH_HEAD"], cwd=output_path
        )

    @staticmethod
    def prepare_repo(
        url: str,
        ref: str,
        hashed_dir: Path,
        name: str,
        git_supports_revision: bool,
    ) -> bool:
        """
        The core logic for the mkdocstrings-multirepo plugin. This clones the git repo
        if it doesn't exist, renames it if the repo does exist but changed name, or
        reuses it if none of the ref/url/name have changed.

        The `bool` return value indicates whether the repo was cloned. This is purely
        so cloning can be logged as it occurs, rather than before it occurs, in the
        main `MkdocstringsMultirepoPlugin.on_config` method. This is necessary as
        cloning is done using `concurrent.futures`.

        Parameters:
            url: The `.git` URL of the repo to clone.
            ref: The ref to checkout in the target repo.
            hashed_dir: A digested SHA256 hash representing the unique combination of
            the `url` and `ref`. This serves as the parent directory to the repo name.
            name: The name of the directory to clone into.
            git_supports_revision: A `bool` indicating whether the machine running
            mkdocs supports the `git clone` `--revision` flag.

        Returns:
            A `bool` indicating whether the repo was cloned (`True`) or renamed/reused
            (`False`).
        """
        output_path = hashed_dir.joinpath(name)
        if not hashed_dir.exists():
            MkdocstringsMultirepoPlugin.clone_git_repo(
                git_supports_revision=git_supports_revision,
                url=url,
                ref=ref,
                output_path=output_path,
            )
            return True
        elif not output_path.exists():
            old_name_path = hashed_dir.joinpath(os.listdir(hashed_dir)[0])
            log.info(
                f"Name change detected. Renaming '{old_name_path}' to '{output_path}'"
            )
            os.rename(old_name_path, output_path)
        else:
            # Hashes are long (64 characters), so this cuts off the last 61 characters
            # so only a sample is shown in the logs.
            hashed_dir_sample = Path(str(hashed_dir)[:-61] + "...")
            log.info(
                f"Reusing repo '{name}' located at '{hashed_dir_sample.joinpath(name)}'"
            )

        return False

    @staticmethod
    def prune_cache(
        configured_repos: list[Path], cached_repos: list[Path], cache_dir: Path
    ) -> None:
        """
        Removes repos currently not in the configuration from the cache.

        Parameters:
            configured_repos: A `list[Path]` of repos currently in the
                mkdocstrings-multirepo configuration.
            cached_repos: A `list[Path]` of all repos currently in the cache.
            cache_dir: The `Path` to mkdocstrings-multirepo's cache directory.
        """
        for repo in cached_repos:
            if repo in configured_repos:
                continue

            if not repo.is_relative_to(cache_dir):
                raise PluginError(
                    "mkdocstrings-multirepo: Almost pruned repo dir "
                    f"{repo}, but it's path is not relative to {cache_dir}."
                )

            shutil.rmtree(repo)
