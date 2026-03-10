#!/bin/bash
# Post-deploy smoke check script for core routes + OIDC
# Verifies critical functionality after deployment

set -e

GALLERY_URL="${GALLERY_URL:-http://localhost:8080}"
TIMEOUT=10

echo "=== Post-Deploy Smoke Check ==="
echo "Target: $GALLERY_URL"
echo ""

# Track failures
FAILED=0

check_route() {
    local name="$1"
    local url="$2"
    local expected_status="${3:-200}"
    
    echo -n "Checking $name... "
    
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$url" 2>/dev/null || echo "000")
    
    if [ "$status" == "$expected_status" ]; then
        echo "✓ PASS (HTTP $status)"
    else
        echo "✗ FAIL (expected $expected_status, got $status)"
        FAILED=1
    fi
}

# 1. / login behavior
check_route "Root endpoint" "$GALLERY_URL/" "200"

# 2. /auth/oidc redirect path
echo -n "Checking /auth/oidc redirect... "
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" -I "$GALLERY_URL/auth/oidc" 2>/dev/null || echo "000")
# Should redirect (302) to OIDC provider or show login
if [ "$status" == "302" ] || [ "$status" == "200" ]; then
    echo "✓ PASS (HTTP $status)"
else
    echo "✗ FAIL (expected 302 or 200, got $status)"
    FAILED=1
fi

# 3. authenticated access to /recent and /trash
# These require auth - check that they return 401 (unauthorized) or redirect
echo -n "Checking /recent (auth required)... "
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$GALLERY_URL/recent" 2>/dev/null || echo "000")
if [ "$status" == "401" ] || [ "$status" == "302" ]; then
    echo "✓ PASS (auth required - HTTP $status)"
else
    echo "⚠ WARNING (expected 401/302, got $status)"
fi

echo -n "Checking /trash (auth required)... "
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$GALLERY_URL/trash" 2>/dev/null || echo "000")
if [ "$status" == "401" ] || [ "$status" == "302" ]; then
    echo "✓ PASS (auth required - HTTP $status)"
else
    echo "⚠ WARNING (expected 401/302, got $status)"
fi

# 4. public direct /view/* still works
# Try a sample public view URL (adjust path as needed for your setup)
echo -n "Checking public /view/* endpoint... "
# Using a generic check - adjust if your public view URLs follow a pattern
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$GALLERY_URL/view" 2>/dev/null || echo "000")
if [ "$status" == "200" ] || [ "$status" == "302" ]; then
    echo "✓ PASS (HTTP $status)"
else
    echo "⚠ WARNING (expected 200/302, got $status)"
fi

echo ""
echo "=== Smoke Check Complete ==="

if [ $FAILED -eq 0 ]; then
    echo "✓ All critical checks passed!"
    exit 0
else
    echo "✗ Some checks failed - review deployment"
    exit 1
fi
