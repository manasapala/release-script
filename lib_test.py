"""Tests for lib"""
from datetime import datetime, timezone
from itertools import product
import os

from requests import Response, HTTPError
import pytest

from github import github_auth_headers
from lib import (
    get_release_pr,
    get_unchecked_authors,
    load_repos_info,
    match_user,
    next_workday_at_10,
    next_versions,
    parse_checkmarks,
    reformatted_full_name,
    ReleasePR,
    upload_to_pypi,
    url_with_access_token,
)
from repo_info import RepoInfo
from test_util import async_wrapper, async_context_manager_yielder, sync_call as call


pytestmark = pytest.mark.asyncio


FAKE_RELEASE_PR_BODY = """

## Alice Pote
  - [x] Implemented AutomaticEmail API ([5de04973](../commit/5de049732f769ec8a2a24068514603f353e13ed4))
  - [ ] Unmarked some files as executable ([c665a2c7](../commit/c665a2c79eaf5e2d54b18f5a880709f5065ed517))

## Nathan Levesque
  - [x] Fixed seed data for naive timestamps (#2712) ([50d19c4a](../commit/50d19c4adf22c5ddc8b8299f4b4579c2b1e35b7f))
  - [garbage] xyz
    """

OTHER_PR = {
    "url": "https://api.github.com/repos/mitodl/micromasters/pulls/2985",
    "html_url": "https://github.com/mitodl/micromasters/pull/2985",
    "body": "not a release",
    "title": "not a release",
    "head": {
        "ref": "other-branch"
    },
}
RELEASE_PR = {
    "url": "https://api.github.com/repos/mitodl/micromasters/pulls/2993",
    "html_url": "https://github.com/mitodl/micromasters/pull/2993",
    "body": FAKE_RELEASE_PR_BODY,
    "title": "Release 0.53.3",
    "head": {
        "ref": "release-candidate"
    },
}
FAKE_PULLS = [OTHER_PR, RELEASE_PR]


async def test_parse_checkmarks():
    """parse_checkmarks should look up the Release PR body and return a list of commits"""
    assert parse_checkmarks(FAKE_RELEASE_PR_BODY) == [
        {
            'checked': True,
            'author_name': 'Alice Pote',
            'title': 'Implemented AutomaticEmail API'
        },
        {
            'checked': False,
            'author_name': 'Alice Pote',
            'title': 'Unmarked some files as executable'
        },
        {
            'checked': True,
            'author_name': 'Nathan Levesque',
            'title': 'Fixed seed data for naive timestamps (#2712)'
        },
    ]


async def test_get_release_pr(mocker):
    """get_release_pr should grab a release from GitHub's API"""
    org = 'org'
    repo = 'repo'
    access_token = 'access'

    get_mock = mocker.async_patch('client_wrapper.ClientWrapper.get', return_value=mocker.Mock(
        json=mocker.Mock(return_value=FAKE_PULLS)
    ))
    pr = await get_release_pr(
        github_access_token=access_token,
        org=org,
        repo=repo,
    )
    get_mock.assert_called_once_with(mocker.ANY, "https://api.github.com/repos/{org}/{repo}/pulls".format(
        org=org,
        repo=repo,
    ), headers=github_auth_headers(access_token))
    assert pr.body == RELEASE_PR['body']
    assert pr.url == RELEASE_PR['html_url']
    assert pr.version == '0.53.3'


async def test_get_release_pr_no_pulls(mocker):
    """If there is no release PR it should return None"""
    mocker.async_patch(
        'client_wrapper.ClientWrapper.get', return_value=mocker.Mock(json=mocker.Mock(return_value=[OTHER_PR]))
    )
    assert await get_release_pr(
        github_access_token='access_token',
        org='org',
        repo='repo-missing',
    ) is None


async def test_too_many_releases(mocker):
    """If there is no release PR, an exception should be raised"""
    pulls = [RELEASE_PR, RELEASE_PR]
    mocker.async_patch(
        'client_wrapper.ClientWrapper.get', return_value=mocker.Mock(json=mocker.Mock(return_value=pulls))
    )
    with pytest.raises(Exception) as ex:
        await get_release_pr(
            github_access_token='access_token',
            org='org',
            repo='repo',
        )

    assert ex.value.args[0] == "More than one pull request for the branch release-candidate"


async def test_no_release_wrong_repo(mocker):
    """If there is no repo accessible, an exception should be raised"""
    response_404 = Response()
    response_404.status_code = 404
    mocker.async_patch(
        'client_wrapper.ClientWrapper.get', return_value=response_404
    )
    with pytest.raises(HTTPError) as ex:
        await get_release_pr(
            github_access_token='access_token',
            org='org',
            repo='repo',
        )

    assert ex.value.response.status_code == 404


async def test_get_unchecked_authors(mocker):
    """
    get_unchecked_authors should download the PR body, parse it,
    filter out checked authors and leave only unchecked ones
    """
    org = 'org'
    repo = 'repo'
    access_token = 'all-access'

    get_release_pr_mock = mocker.async_patch('lib.get_release_pr', return_value=ReleasePR(
        body=FAKE_RELEASE_PR_BODY,
        version='1.2.3',
        url='http://url'
    ))
    unchecked = await get_unchecked_authors(
        github_access_token=access_token,
        org=org,
        repo=repo,
    )
    assert unchecked == {"Alice Pote"}
    get_release_pr_mock.assert_called_once_with(
        github_access_token=access_token,
        org=org,
        repo=repo,
    )


async def test_next_workday_at_10():
    """next_workday_at_10 should get the time that's tomorrow at 10am, or Monday if that's the next workday"""
    saturday_at_8am = datetime(2017, 4, 1, 8, tzinfo=timezone.utc)
    assert next_workday_at_10(saturday_at_8am) == datetime(2017, 4, 3, 10, tzinfo=timezone.utc)
    tuesday_at_4am = datetime(2017, 4, 4, 4, tzinfo=timezone.utc)
    assert next_workday_at_10(tuesday_at_4am) == datetime(2017, 4, 5, 10, tzinfo=timezone.utc)
    wednesday_at_3pm = datetime(2017, 4, 5, 15, tzinfo=timezone.utc)
    assert next_workday_at_10(wednesday_at_3pm) == datetime(2017, 4, 6, 10, tzinfo=timezone.utc)


async def test_reformatted_full_name():
    """reformatted_full_name should take the first and last names and make it lowercase"""
    assert reformatted_full_name("") == ""
    assert reformatted_full_name("George") == "george"
    assert reformatted_full_name("X Y Z A B") == "x b"


FAKE_SLACK_USERS = [
    {
        'profile': {
            'real_name': 'George Schneeloch',
        },
        'id': 'U12345',
    },
    {
        'profile': {
            'real_name': 'Sar Haidar'
        },
        'id': 'U65432'
    },
    {
        'profile': {
            'real_name': 'Sarah H'
        },
        'id': 'U13986'
    },
    {
        'profile': {
            'real_name': 'Tasawer Nawaz'
        },
        'id': 'U9876'
    }
]


async def test_match_users():
    """match_users should use the Levensthein distance to compare usernames"""
    assert match_user(FAKE_SLACK_USERS, "George Schneeloch") == "<@U12345>"
    assert match_user(FAKE_SLACK_USERS, "George Schneelock") == "<@U12345>"
    assert match_user(FAKE_SLACK_USERS, "George") == "<@U12345>"
    assert match_user(FAKE_SLACK_USERS, 'sar') == '<@U65432>'
    assert match_user(FAKE_SLACK_USERS, 'tasawernawaz') == '<@U9876>'


async def test_url_with_access_token():
    """url_with_access_token should insert the access token into the url"""
    assert url_with_access_token(
        "access", "http://github.com/mitodl/release-script.git"
    ) == "https://access@github.com/mitodl/release-script.git"


# pylint: disable=too-many-arguments
@pytest.mark.parametrize("testing,python2,python3", list(product([True, False], repeat=3)))
async def test_upload_to_pypi(testing, python2, python3, mocker, library_test_repo, test_repo_directory):
    """upload_to_pypi should create a dist based on a version and upload to pypi or pypitest"""

    twine_env = {
        'PYPI_USERNAME': 'user',
        'PYPI_PASSWORD': 'pass',
        'PYPITEST_USERNAME': 'testuser',
        'PYPITEST_PASSWORD': 'testpass',
    }
    library_test_repo = RepoInfo(**{
        **library_test_repo._asdict(),
        'python2': python2,
        'python3': python3,
    })

    def check_call_func(*args, env, **kwargs):
        """Patch check_call to skip twine"""
        command_args = args[0]

        env = {
            **os.environ,
            **env
        } if env is not None else os.environ

        if not command_args[0].endswith("twine"):
            call(*args, **kwargs, env=env)

        if command_args[0].endswith("twine"):
            # Need to assert twine here since temp directory is random which makes it assert to assert
            assert env['TWINE_USERNAME'] == (
                twine_env['PYPITEST_USERNAME'] if testing else twine_env['PYPI_USERNAME']
            )
            assert env['TWINE_PASSWORD'] == (
                twine_env['PYPITEST_PASSWORD'] if testing else twine_env['PYPI_PASSWORD']
            )
            assert args[0][1] == 'upload'
            if testing:
                assert args[0][2:4] == ["--repository-url", "https://test.pypi.org/legacy/"]
            dist_files = args[0][-2:]
            assert len([name for name in dist_files if name.endswith(".whl")]) == 1
            assert len([name for name in dist_files if name.endswith(".tar.gz")]) == 1

        if 'bdist_wheel' in command_args:
            if python2 and python3:
                assert "--universal" in command_args

    mocker.async_patch('lib.check_call', side_effect=check_call_func)
    call_mock = mocker.async_patch('lib.call', side_effect=check_call_func)
    mocker.async_patch('lib.check_output', return_value=b'x=y=z')
    mocker.patch.dict('os.environ', twine_env, clear=False)

    init_working_dir_mock = mocker.patch(
        'lib.init_working_dir', side_effect=async_context_manager_yielder(test_repo_directory)
    )

    version = "4.5.6"
    token = "token"
    await upload_to_pypi(
        repo_info=library_test_repo, testing=testing, github_access_token=token, version=version
    )
    assert call_mock.call_args[1] == {'env': {'x': 'y=z'}, 'cwd': test_repo_directory}
    init_working_dir_mock.assert_called_once_with(token, library_test_repo.repo_url, branch=f"v{version}")


async def test_load_repos_info(mocker):
    """
    load_repos_info should match channels with repositories
    """
    json_load = mocker.patch('lib.json.load', autospec=True, return_value={
        'repos': [
            {
                "name": "bootcamp-ecommerce",
                "repo_url": "https://github.com/mitodl/bootcamp-ecommerce.git",
                "rc_hash_url": "https://bootcamp-ecommerce-rc.herokuapp.com/static/hash.txt",
                "prod_hash_url": "https://bootcamp-ecommerce.herokuapp.com/static/hash.txt",
                "channel_name": "bootcamp-eng",
                "project_type": "web_application",
                "python2": False,
                "python3": True,
                "announcements": False,
            }
        ]
    })

    assert load_repos_info({
        'bootcamp-eng': 'bootcamp_channel_id'
    }) == [
        RepoInfo(
            name='bootcamp-ecommerce',
            repo_url='https://github.com/mitodl/bootcamp-ecommerce.git',
            rc_hash_url="https://bootcamp-ecommerce-rc.herokuapp.com/static/hash.txt",
            prod_hash_url="https://bootcamp-ecommerce.herokuapp.com/static/hash.txt",
            channel_id='bootcamp_channel_id',
            project_type='web_application',
            python2=False,
            python3=True,
            announcements=False,
        ),
    ]
    assert json_load.call_count == 1


async def test_next_versions():
    """next_versions should return a tuple of the updated minor and patch versions"""
    assert next_versions("1.2.3") == ("1.3.0", "1.2.4")


async def test_async_wrapper(mocker):
    """async_wrapper should convert a sync function into a trivial async function"""
    func = mocker.Mock()
    async_func = async_wrapper(func)
    await async_func()
    await async_func()
    assert func.call_count == 2


async def test_async_patch(mocker):
    """async_patch should patch with an async function"""
    mocked = mocker.async_patch('lib_test.call')
    mocked.return_value = 123
    assert await call(["ls"], cwd="/") == 123
    assert await call(["ls"], cwd="/") == 123
