class Permit2Handler:
    async def generate_signature(self, token: str, amount: int, spender: str) -> str:
        # lightweight stub for tests; your real handler will sign properly
        return "0x" + "ab"*32
