import asyncio
from anchorpy import Program, Provider, Wallet
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

RPC_URL = "https://api.mainnet-beta.solana.com"
KLEND_PROGRAM_ID = Pubkey.from_string("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")

async def fetch_idl():
    client = AsyncClient(RPC_URL)
    provider = Provider(client, Wallet.dummy())
    idl = await Program.fetch_idl(KLEND_PROGRAM_ID, provider)
    await client.close()
    if idl is None:
        print("Nessun IDL pubblicato on-chain per questo programma.")
    else:
        print("IDL trovato! Nome:", idl.name)
        with open("klend_idl.json", "w") as f:
            f.write(idl.to_json())
        print("Salvato in klend_idl.json")
    return idl

if __name__ == "__main__":
    asyncio.run(fetch_idl())
