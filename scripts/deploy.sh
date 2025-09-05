#!/bin/bash
# deploy.sh - Production deployment script

set -e  # Exit on error

echo "🚀 MEV Bot Production Deployment"
echo "================================"

# Load environment
source .env.production

# Run pre-deployment checks
echo "📋 Running pre-deployment checks..."
python3 deployment/production_checklist.py
if [ $? -ne 0 ]; then
    echo "❌ Pre-deployment checks failed"
    exit 1
fi

# Backup current deployment
echo "💾 Backing up current deployment..."
docker-compose down
tar -czf backups/backup-$(date +%Y%m%d-%H%M%S).tar.gz \
    config/ \
    logs/ \
    data/

# Pull latest code
echo "📥 Pulling latest code..."
git pull origin main
git submodule update --init --recursive

# Build containers
echo "🔨 Building containers..."
docker-compose build --no-cache

# Run database migrations
echo "🗄️ Running database migrations..."
docker-compose run --rm mev-bot python manage.py migrate

# Start services
echo "🚀 Starting services..."
docker-compose up -d

# Wait for services to be healthy
echo "⏳ Waiting for services..."
sleep 10

# Run smoke tests
echo "🧪 Running smoke tests..."
python3 tests/smoke_tests.py

# Check service health
echo "❤️ Checking service health..."
docker-compose ps
curl -f http://localhost:8000/health || exit 1

echo "✅ Deployment successful!"
echo ""
echo "📊 Dashboard: http://localhost:3000"
echo "📈 Prometheus: http://localhost:9090"
echo "🤖 API: http://localhost:8000"
