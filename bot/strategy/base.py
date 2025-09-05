from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Dict, Any

class ExecutionMode(Enum):
    STEALTH = "stealth"
    HUNTER = "hunter"
    HYBRID = "hybrid"

class BaseStrategy(ABC):
    @abstractmethod
    async def evaluate(self, context: Dict[str, Any]) -> float:
        """Return profit score 0-1"""
        pass

    @abstractmethod
    async def execute(self, opportunity: Dict[str, Any]) -> TransactionResult:
        pass

class StealthStrategy(BaseStrategy):
    """
    Invisible execution strategy to avoid MEV attacks
    """
    def __init__(self):
        self.private_rpcs = {
            'ethereum': ['flashbots_protect', 'mev_blocker'],
            'polygon': ['polygon_flashbots'],
            'base': ['base_private_pool']
        }
        self.permit2_handler = Permit2Handler()

    async def should_go_stealth(self, trade: Dict) -> bool:
        """Determine if trade should use stealth mode"""
        conditions = [
            trade['estimated_slippage'] > 0.005,  # >0.5% slippage
            trade['token_age_hours'] < 24,        # New token
            trade['liquidity_usd'] < 100000,      # Low liquidity
            trade['is_trending'],                  # High attention
            trade['detected_snipers'] > 0         # Active snipers
        ]
        return sum(conditions) >= 2

    async def execute_stealth_swap(self, params: Dict) -> TransactionResult:
        # 1. Generate Permit2 signature off-chain
        permit_sig = await self.permit2_handler.generate_signature(
            token=params['token_in'],
            amount=params['amount'],
            spender=params['router']
        )

        # 2. Build exact-output swap
        swap_data = self.build_exact_output_swap(
            token_in=params['token_in'],
            token_out=params['token_out'],
            amount_out_exact=params['desired_output'],
            max_amount_in=params['max_input'],
            permit_signature=permit_sig
        )

        # 3. Submit via private RPC
        result = await self.submit_private_transaction(
            rpc=self.select_best_private_rpc(params['chain']),
            tx_data=swap_data,
            max_priority_fee=params['max_priority_fee']
        )

        return result

class HunterStrategy(BaseStrategy):
    """
    Active MEV extraction via backrunning
    """
    def __init__(self):
        self.mempool_monitor = MempoolMonitor()
        self.bundle_builder = BundleBuilder()

    async def detect_sniper_opportunity(self, tx: PendingTransaction) -> Optional[Opportunity]:
        # Pattern matching for sniper detection
        if self.is_sandwich_attempt(tx):
            return self.calculate_backrun_opportunity(tx)
        return None

    async def execute_backrun(self, sniper_tx: Transaction, opportunity: Opportunity):
        # Build atomic bundle
        bundle = self.bundle_builder.create_bundle([
            sniper_tx,  # Let sniper go first
            self.create_backrun_tx(opportunity)  # Our profitable backrun
        ])

        # Submit to builders
        return await self.submit_to_builders(bundle)
