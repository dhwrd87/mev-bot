# deployment/production_checklist.py

class ProductionDeployment:
    """
    Production deployment validator and orchestrator
    """

    def __init__(self):
        self.checks = []
        self.warnings = []
        self.errors = []

    async def pre_deployment_checks(self) -> bool:
        """Run all pre-deployment validation"""

        checks = [
            self.check_environment_variables(),
            self.check_rpc_endpoints(),
            self.check_private_keys(),
            self.check_database_connectivity(),
            self.check_monitoring_setup(),
            self.check_risk_limits(),
            self.check_backup_systems(),
            self.check_emergency_procedures()
        ]

        results = await asyncio.gather(*checks)
        return all(results)

    async def check_environment_variables(self) -> bool:
        """Validate all required environment variables"""
        required_vars = [
            'CHAIN_ID',
            'RPC_ENDPOINT_PRIMARY',
            'RPC_ENDPOINT_BACKUP',
            'FLASHBOTS_RELAY_URL',
            'PRIVATE_KEY_ENCRYPTED',
            'KEY_PASSWORD',
            'DISCORD_WEBHOOK',
            'MAX_DAILY_LOSS',
            'MAX_POSITION_SIZE'
        ]

        missing = []
        for var in required_vars:
            if not os.getenv(var):
                missing.append(var)
                self.errors.append(f"Missing required env var: {var}")

        return len(missing) == 0

    async def check_rpc_endpoints(self) -> bool:
        """Test RPC endpoint connectivity and latency"""
        endpoints = [
            os.getenv('RPC_ENDPOINT_PRIMARY'),
            os.getenv('RPC_ENDPOINT_BACKUP')
        ]

        for endpoint in endpoints:
            try:
                w3 = Web3(HTTPProvider(endpoint))

                # Test basic connectivity
                if not w3.isConnected():
                    self.errors.append(f"Cannot connect to RPC: {endpoint}")
                    return False

                # Test latency
                start = time.time()
                block = w3.eth.get_block('latest')
                latency = (time.time() - start) * 1000

                if latency > 100:  # >100ms is too slow
                    self.warnings.append(f"High RPC latency: {latency:.0f}ms for {endpoint}")

                # Test mempool access
                try:
                    pending = w3.eth.get_block('pending', full_transactions=True)
                    if len(pending.transactions) == 0:
                        self.warnings.append(f"No pending transactions visible on {endpoint}")
                except:
                    self.errors.append(f"Cannot access mempool on {endpoint}")

            except Exception as e:
                self.errors.append(f"RPC check failed: {str(e)}")
                return False

        return True

    async def validate_strategy_configs(self) -> bool:
        """Validate strategy configuration files"""
        config_path = Path('./config/strategies.yaml')

        if not config_path.exists():
            self.errors.append("Strategy config file not found")
            return False

        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Validate stealth strategy config
        stealth = config.get('stealth_strategy', {})
        if not stealth.get('private_rpcs'):
            self.errors.append("No private RPCs configured for stealth mode")
            return False

        if stealth.get('max_slippage', 1.0) > 0.02:
            self.warnings.append("High max slippage configured for stealth mode")

        # Validate hunter strategy config
        hunter = config.get('hunter_strategy', {})
        if hunter.get('min_profit_wei', 0) < 100000000000000:  # 0.0001 ETH
            self.warnings.append("Very low minimum profit threshold")

        return True
