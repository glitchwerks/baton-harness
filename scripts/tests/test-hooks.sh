#!/usr/bin/env bash
# scripts/tests/test-hooks.sh — unit/integration tests for after-create.sh and before-run.sh
#
# Run from the worktree root:
#   bash scripts/tests/test-hooks.sh
#
# Uses a minimal homegrown test harness (no bats dependency).

set -euo pipefail

WORKTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS_DIR="${WORKTREE_ROOT}/scripts"
SCRATCH_DIR="${WORKTREE_ROOT}/.tmp/test-hooks-$$"
PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Minimal test harness
# ---------------------------------------------------------------------------

_pass() { echo "  PASS: $1"; (( PASS++ )) || true; }
_fail() { echo "  FAIL: $1"; echo "        $2"; (( FAIL++ )) || true; }

run_test() {
    local description="$1"
    local test_fn="$2"
    if "${test_fn}"; then
        _pass "${description}"
    else
        _fail "${description}" "function returned non-zero"
    fi
}

assert_exit_zero() {
    local cmd=("$@")
    if "${cmd[@]}"; then
        return 0
    else
        echo "    expected exit 0; got $?"
        return 1
    fi
}

assert_exit_nonzero() {
    local cmd=("$@")
    if "${cmd[@]}" 2>/dev/null; then
        echo "    expected non-zero exit; got 0"
        return 1
    else
        return 0
    fi
}

assert_output_contains() {
    local pattern="$1"; shift
    local output
    output=$("$@" 2>&1) || true
    if echo "${output}" | grep -q "${pattern}"; then
        return 0
    else
        echo "    output did not contain '${pattern}'"
        echo "    actual output: ${output}"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Setup / teardown helpers
# ---------------------------------------------------------------------------

setup_scratch() {
    mkdir -p "${SCRATCH_DIR}"
}

teardown_scratch() {
    rm -rf "${SCRATCH_DIR}"
}

make_fake_git_repo() {
    local dir="$1"
    mkdir -p "${dir}"
    git -C "${dir}" init -q
    git -C "${dir}" config user.email "test@test.com"
    git -C "${dir}" config user.name "Test"
    # initial commit so there is a branch
    echo "init" > "${dir}/README.md"
    git -C "${dir}" add README.md
    git -C "${dir}" commit -q -m "init"
}

# ---------------------------------------------------------------------------
# after-create.sh tests
# ---------------------------------------------------------------------------

test_after_create_exists() {
    [[ -f "${SCRIPTS_DIR}/after-create.sh" ]]
}

test_after_create_executable() {
    [[ -x "${SCRIPTS_DIR}/after-create.sh" ]]
}

test_after_create_syntax() {
    bash -n "${SCRIPTS_DIR}/after-create.sh"
}

test_after_create_no_deps_noop() {
    # A directory with no package.json, requirements.txt, or pyproject.toml
    # should exit 0 with an informative no-op message
    local tmp="${SCRATCH_DIR}/no-deps"
    mkdir -p "${tmp}"
    local output
    output=$(cd "${tmp}" && bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    local status=$?
    if [[ ${status} -ne 0 ]]; then
        echo "    expected exit 0 for no-deps dir; got ${status}"
        return 1
    fi
    if ! echo "${output}" | grep -qi "no dependency"; then
        echo "    expected a 'no dependency' log line; got: ${output}"
        return 1
    fi
}

test_after_create_package_json_no_lockfile() {
    # package.json present, no package-lock.json → should run npm install
    # We cannot actually run npm install in tests, so we verify the script
    # selects the right command by dry-running with a stub npm on PATH.
    local tmp="${SCRATCH_DIR}/npm-no-lock"
    mkdir -p "${tmp}/bin"
    echo '{}' > "${tmp}/package.json"
    # Stub npm that records args
    cat > "${tmp}/bin/npm" <<'EOF'
#!/usr/bin/env bash
echo "STUB_NPM $*"
exit 0
EOF
    chmod +x "${tmp}/bin/npm"
    local output
    output=$(cd "${tmp}" && PATH="${tmp}/bin:${PATH}" bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -q "STUB_NPM install"; then
        echo "    expected 'npm install' to be called; got: ${output}"
        return 1
    fi
    if echo "${output}" | grep -q "STUB_NPM ci"; then
        echo "    expected 'npm install' (not ci) when no lockfile; got: ${output}"
        return 1
    fi
}

test_after_create_package_json_with_lockfile() {
    # package.json + package-lock.json present → should run npm ci
    local tmp="${SCRATCH_DIR}/npm-with-lock"
    mkdir -p "${tmp}/bin"
    echo '{}' > "${tmp}/package.json"
    touch "${tmp}/package-lock.json"
    cat > "${tmp}/bin/npm" <<'EOF'
#!/usr/bin/env bash
echo "STUB_NPM $*"
exit 0
EOF
    chmod +x "${tmp}/bin/npm"
    local output
    output=$(cd "${tmp}" && PATH="${tmp}/bin:${PATH}" bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -q "STUB_NPM ci"; then
        echo "    expected 'npm ci' when lockfile present; got: ${output}"
        return 1
    fi
}

test_after_create_requirements_txt_uv_preferred() {
    # requirements.txt present, uv available → should use 'uv pip install'
    local tmp="${SCRATCH_DIR}/python-uv"
    mkdir -p "${tmp}/bin"
    echo "requests" > "${tmp}/requirements.txt"
    cat > "${tmp}/bin/uv" <<'EOF'
#!/usr/bin/env bash
echo "STUB_UV $*"
exit 0
EOF
    chmod +x "${tmp}/bin/uv"
    local output
    output=$(cd "${tmp}" && PATH="${tmp}/bin:${PATH}" bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -q "STUB_UV pip install"; then
        echo "    expected 'uv pip install' when uv available; got: ${output}"
        return 1
    fi
}

test_after_create_requirements_txt_pip_fallback() {
    # requirements.txt present, uv NOT on PATH → should fall back to 'pip install'
    local tmp="${SCRATCH_DIR}/python-pip"
    mkdir -p "${tmp}/bin"
    echo "requests" > "${tmp}/requirements.txt"
    cat > "${tmp}/bin/pip" <<'EOF'
#!/usr/bin/env bash
echo "STUB_PIP $*"
exit 0
EOF
    chmod +x "${tmp}/bin/pip"
    # PATH has pip but no uv; retain system directories so bash builtins work
    local output
    output=$(cd "${tmp}" && PATH="${tmp}/bin:/usr/bin:/bin" bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -q "STUB_PIP install"; then
        echo "    expected 'pip install' fallback when uv absent; got: ${output}"
        return 1
    fi
}

test_after_create_pyproject_toml_editable() {
    # pyproject.toml present → should install with -e . or -e ".[dev]"
    local tmp="${SCRATCH_DIR}/python-pyproject"
    mkdir -p "${tmp}/bin"
    cat > "${tmp}/pyproject.toml" <<'EOF'
[project]
name = "example"
version = "0.1.0"
EOF
    cat > "${tmp}/bin/uv" <<'EOF'
#!/usr/bin/env bash
echo "STUB_UV $*"
exit 0
EOF
    chmod +x "${tmp}/bin/uv"
    local output
    output=$(cd "${tmp}" && PATH="${tmp}/bin:${PATH}" bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -qE "STUB_UV pip install.*-e"; then
        echo "    expected editable install (-e) for pyproject.toml; got: ${output}"
        return 1
    fi
}

test_after_create_derives_issue_from_cwd() {
    # When run from a worktree-named dir, issue number appears in log output
    local tmp="${SCRATCH_DIR}/42"
    mkdir -p "${tmp}"
    local output
    output=$(cd "${tmp}" && bash "${SCRIPTS_DIR}/after-create.sh" 2>&1)
    if ! echo "${output}" | grep -q "42"; then
        echo "    expected issue number '42' in log output; got: ${output}"
        return 1
    fi
}

test_after_create_explicit_issue_arg() {
    # When issue number passed explicitly, it appears in log output
    local tmp="${SCRATCH_DIR}/no-deps-arg"
    mkdir -p "${tmp}"
    local output
    output=$(cd "${tmp}" && bash "${SCRIPTS_DIR}/after-create.sh" 99 2>&1)
    if ! echo "${output}" | grep -q "99"; then
        echo "    expected issue number '99' in log output; got: ${output}"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# before-run.sh tests
# ---------------------------------------------------------------------------

test_before_run_exists() {
    [[ -f "${SCRIPTS_DIR}/before-run.sh" ]]
}

test_before_run_executable() {
    [[ -x "${SCRIPTS_DIR}/before-run.sh" ]]
}

test_before_run_syntax() {
    bash -n "${SCRIPTS_DIR}/before-run.sh"
}

test_before_run_fetch_and_rebase() {
    # In a repo where origin/main exists and branch is behind, rebase succeeds.
    # Set up: local repo + bare remote so we can test git operations end-to-end.
    local remote="${SCRATCH_DIR}/remote.git"
    local local_repo="${SCRATCH_DIR}/local-repo"

    # Create a bare remote
    git init -q --bare "${remote}"

    # Clone and set up main
    git clone -q "${remote}" "${local_repo}"
    git -C "${local_repo}" config user.email "test@test.com"
    git -C "${local_repo}" config user.name "Test"
    echo "init" > "${local_repo}/README.md"
    git -C "${local_repo}" add README.md
    git -C "${local_repo}" commit -q -m "init"
    git -C "${local_repo}" push -q origin HEAD:main

    # Create a feature branch and add a commit
    git -C "${local_repo}" checkout -q -b "feat/issue-7"
    echo "feature" > "${local_repo}/feature.txt"
    git -C "${local_repo}" add feature.txt
    git -C "${local_repo}" commit -q -m "feat: feature work"

    # Advance main on the remote (simulates other work merged to main)
    git -C "${local_repo}" checkout -q main
    echo "main-advance" > "${local_repo}/main-file.txt"
    git -C "${local_repo}" add main-file.txt
    git -C "${local_repo}" commit -q -m "chore: advance main"
    git -C "${local_repo}" push -q origin main

    # Switch back to feature branch
    git -C "${local_repo}" checkout -q "feat/issue-7"

    # Run before-run.sh from within the feature branch worktree
    local output
    output=$(cd "${local_repo}" && bash "${SCRIPTS_DIR}/before-run.sh" 2>&1)
    local status=$?

    if [[ ${status} -ne 0 ]]; then
        echo "    expected exit 0 for clean rebase; got ${status}"
        echo "    output: ${output}"
        return 1
    fi

    # Verify the feature branch was rebased (main-file.txt should now exist)
    if [[ ! -f "${local_repo}/main-file.txt" ]]; then
        echo "    expected main-file.txt after rebase (branch not updated)"
        return 1
    fi
}

test_before_run_idempotent_already_up_to_date() {
    # Running before-run.sh twice should succeed both times.
    local remote="${SCRATCH_DIR}/remote2.git"
    local local_repo="${SCRATCH_DIR}/local-repo2"

    git init -q --bare "${remote}"
    git clone -q "${remote}" "${local_repo}"
    git -C "${local_repo}" config user.email "test@test.com"
    git -C "${local_repo}" config user.name "Test"
    echo "init" > "${local_repo}/README.md"
    git -C "${local_repo}" add README.md
    git -C "${local_repo}" commit -q -m "init"
    git -C "${local_repo}" push -q origin HEAD:main

    # Feature branch on top of already-current main
    git -C "${local_repo}" checkout -q -b "feat/issue-8"
    echo "feature" > "${local_repo}/feature.txt"
    git -C "${local_repo}" add feature.txt
    git -C "${local_repo}" commit -q -m "feat: work"

    local output1 status1 output2 status2
    output1=$(cd "${local_repo}" && bash "${SCRIPTS_DIR}/before-run.sh" 2>&1); status1=$?
    output2=$(cd "${local_repo}" && bash "${SCRIPTS_DIR}/before-run.sh" 2>&1); status2=$?

    if [[ ${status1} -ne 0 ]]; then
        echo "    first run failed (${status1}): ${output1}"
        return 1
    fi
    if [[ ${status2} -ne 0 ]]; then
        echo "    second run failed (${status2}): ${output2}"
        return 1
    fi
}

test_before_run_conflict_aborts_and_exits_nonzero() {
    # If rebase conflicts, script should abort and exit non-zero.
    local remote="${SCRATCH_DIR}/remote3.git"
    local local_repo="${SCRATCH_DIR}/local-repo3"

    git init -q --bare "${remote}"
    git clone -q "${remote}" "${local_repo}"
    git -C "${local_repo}" config user.email "test@test.com"
    git -C "${local_repo}" config user.name "Test"
    echo "line1" > "${local_repo}/conflict.txt"
    git -C "${local_repo}" add conflict.txt
    git -C "${local_repo}" commit -q -m "init"
    git -C "${local_repo}" push -q origin HEAD:main

    # Create feature branch that edits conflict.txt
    git -C "${local_repo}" checkout -q -b "feat/issue-9"
    echo "feature-change" > "${local_repo}/conflict.txt"
    git -C "${local_repo}" add conflict.txt
    git -C "${local_repo}" commit -q -m "feat: conflicting change"

    # Advance main with a conflicting change to the same file
    git -C "${local_repo}" checkout -q main
    echo "main-change" > "${local_repo}/conflict.txt"
    git -C "${local_repo}" add conflict.txt
    git -C "${local_repo}" commit -q -m "chore: conflicting main change"
    git -C "${local_repo}" push -q origin main

    git -C "${local_repo}" checkout -q "feat/issue-9"

    local output status
    output=$(cd "${local_repo}" && bash "${SCRIPTS_DIR}/before-run.sh" 2>&1) || status=$?
    status=${status:-0}

    if [[ ${status} -eq 0 ]]; then
        echo "    expected non-zero exit on conflict; got 0"
        echo "    output: ${output}"
        return 1
    fi

    # Repo should not be mid-rebase after the abort
    if [[ -d "${local_repo}/.git/rebase-merge" ]] || [[ -d "${local_repo}/.git/rebase-apply" ]]; then
        echo "    repo is still mid-rebase after script ran (abort did not clean up)"
        return 1
    fi
}

test_before_run_exits_nonzero_with_clear_message_on_conflict() {
    # The error message on conflict should be human-readable (not a raw git error dump).
    local remote="${SCRATCH_DIR}/remote4.git"
    local local_repo="${SCRATCH_DIR}/local-repo4"

    git init -q --bare "${remote}"
    git clone -q "${remote}" "${local_repo}"
    git -C "${local_repo}" config user.email "test@test.com"
    git -C "${local_repo}" config user.name "Test"
    echo "original" > "${local_repo}/file.txt"
    git -C "${local_repo}" add file.txt
    git -C "${local_repo}" commit -q -m "init"
    git -C "${local_repo}" push -q origin HEAD:main

    git -C "${local_repo}" checkout -q -b "feat/issue-10"
    echo "feat-version" > "${local_repo}/file.txt"
    git -C "${local_repo}" add file.txt
    git -C "${local_repo}" commit -q -m "feat: change file"

    git -C "${local_repo}" checkout -q main
    echo "main-version" > "${local_repo}/file.txt"
    git -C "${local_repo}" add file.txt
    git -C "${local_repo}" commit -q -m "chore: main change"
    git -C "${local_repo}" push -q origin main

    git -C "${local_repo}" checkout -q "feat/issue-10"

    local output
    output=$(cd "${local_repo}" && bash "${SCRIPTS_DIR}/before-run.sh" 2>&1) || true

    if ! echo "${output}" | grep -qi "conflict\|rebase.*fail\|abort"; then
        echo "    expected a conflict/abort message in output; got: ${output}"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

echo ""
echo "=== Hook script tests ==="
echo ""

setup_scratch

echo "--- after-create.sh ---"
run_test "script file exists"                           test_after_create_exists
run_test "script is executable"                         test_after_create_executable
run_test "syntax check (bash -n)"                       test_after_create_syntax
run_test "no deps → exit 0 with no-op log"              test_after_create_no_deps_noop
run_test "package.json, no lockfile → npm install"      test_after_create_package_json_no_lockfile
run_test "package.json + lock → npm ci"                 test_after_create_package_json_with_lockfile
run_test "requirements.txt, uv present → uv pip install" test_after_create_requirements_txt_uv_preferred
run_test "requirements.txt, no uv → pip install"        test_after_create_requirements_txt_pip_fallback
run_test "pyproject.toml → editable install (-e)"       test_after_create_pyproject_toml_editable
run_test "derives issue number from cwd basename"        test_after_create_derives_issue_from_cwd
run_test "accepts explicit issue arg"                   test_after_create_explicit_issue_arg

echo ""
echo "--- before-run.sh ---"
run_test "script file exists"                           test_before_run_exists
run_test "script is executable"                         test_before_run_executable
run_test "syntax check (bash -n)"                       test_before_run_syntax
run_test "fetch + rebase onto origin/main"              test_before_run_fetch_and_rebase
run_test "idempotent: already up-to-date succeeds"      test_before_run_idempotent_already_up_to_date
run_test "conflict → abort rebase + exit non-zero"      test_before_run_conflict_aborts_and_exits_nonzero
run_test "conflict → clear error message in output"     test_before_run_exits_nonzero_with_clear_message_on_conflict

teardown_scratch

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
echo ""

[[ ${FAIL} -eq 0 ]]
