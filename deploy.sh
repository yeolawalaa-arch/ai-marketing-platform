#!/bin/bash
set -e

RAILWAY="$HOME/.railway/bin/railway"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "🚀 AI Marketing Hub — Auto Deploy Script"
echo "========================================="
echo ""

# 1. Install Railway CLI if missing
if [ ! -f "$RAILWAY" ]; then
  echo "📦 Installing Railway CLI..."
  curl -fsSL https://railway.app/install.sh | sh
  echo -e "${GREEN}✅ Railway CLI installed${NC}"
fi

# 2. Login to Railway
echo ""
echo "🔐 Step 1: Login to Railway"
echo "   → A browser window will open. Login with Google or GitHub."
echo "   → Come back here after logging in."
echo ""
$RAILWAY login

# 3. Create project
echo ""
echo "📁 Step 2: Creating Railway project..."
cd "$(dirname "$0")"
$RAILWAY init --name "ai-marketing-hub"
echo -e "${GREEN}✅ Project created${NC}"

# 4. Add PostgreSQL database
echo ""
echo "🗄️  Step 3: Adding free PostgreSQL database..."
$RAILWAY add --database postgres
echo -e "${GREEN}✅ Database added${NC}"

# 5. Collect environment variables
echo ""
echo "🔑 Step 4: Configure your settings"
echo ""

read -p "   Enter your Groq API Key (from console.groq.com): " GROQ_KEY
while [ -z "$GROQ_KEY" ]; do
  echo -e "${RED}   ⚠️  Groq key is required for the AI to work${NC}"
  read -p "   Enter your Groq API Key: " GROQ_KEY
done

read -p "   Pro plan price in ₹ (press Enter for 999): " PRICE
PRICE=${PRICE:-999}

read -p "   Razorpay Key ID (press Enter to skip for now): " RZP_KEY_ID
read -p "   Razorpay Key Secret (press Enter to skip for now): " RZP_SECRET
read -p "   Razorpay Plan ID (press Enter to skip for now): " RZP_PLAN

# Generate a random secret key
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 6. Set environment variables
echo ""
echo "⚙️  Step 5: Setting environment variables..."
$RAILWAY variables set SECRET_KEY="$SECRET"
$RAILWAY variables set GROQ_API_KEY="$GROQ_KEY"
$RAILWAY variables set PRO_PRICE_INR="$PRICE"
$RAILWAY variables set RAILWAY_ENVIRONMENT="production"

if [ -n "$RZP_KEY_ID" ]; then
  $RAILWAY variables set RAZORPAY_KEY_ID="$RZP_KEY_ID"
fi
if [ -n "$RZP_SECRET" ]; then
  $RAILWAY variables set RAZORPAY_KEY_SECRET="$RZP_SECRET"
fi
if [ -n "$RZP_PLAN" ]; then
  $RAILWAY variables set RAZORPAY_PLAN_ID="$RZP_PLAN"
fi

echo -e "${GREEN}✅ Variables set${NC}"

# 7. Deploy
echo ""
echo "🚀 Step 6: Deploying your app..."
$RAILWAY up --detach
echo -e "${GREEN}✅ Deployment started${NC}"

# 8. Get live URL
echo ""
echo "🌐 Step 7: Getting your live HTTPS URL..."
sleep 5
DOMAIN=$($RAILWAY domain 2>/dev/null || echo "")

if [ -z "$DOMAIN" ]; then
  $RAILWAY domain generate 2>/dev/null || true
  sleep 3
  DOMAIN=$($RAILWAY domain 2>/dev/null || echo "")
fi

echo ""
echo "======================================================="
echo -e "${GREEN}🎉 YOUR APP IS LIVE!${NC}"
echo "======================================================="
if [ -n "$DOMAIN" ]; then
  echo -e "   🔗 ${GREEN}https://${DOMAIN}${NC}"
else
  echo "   Run: ~/.railway/bin/railway domain"
  echo "   to get your live URL"
fi
echo ""
echo "   📊 Dashboard:  ~/.railway/bin/railway open"
echo "   📝 Logs:       ~/.railway/bin/railway logs"
echo "   🔄 Redeploy:   ~/.railway/bin/railway up"
echo ""
if [ -z "$RZP_KEY_ID" ]; then
  echo -e "${YELLOW}⚠️  Razorpay not configured yet.${NC}"
  echo "   Get keys from razorpay.com then run:"
  echo "   ~/.railway/bin/railway variables set RAZORPAY_KEY_ID=rzp_live_xxx"
  echo "   ~/.railway/bin/railway variables set RAZORPAY_KEY_SECRET=xxx"
  echo "   ~/.railway/bin/railway variables set RAZORPAY_PLAN_ID=plan_xxx"
fi
echo "======================================================="
