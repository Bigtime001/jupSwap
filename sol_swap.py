
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.message import Message
from solana.rpc.api import Client
import base58
import requests
import json
import base64
import time
import aiohttp
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HELIUS_URL = "https://mainnet.helius-rpc.com/?api-key=2ea68573-e4c1-48ec-a2bd-7baa385c7698"
SOL_ADDRESS = "So11111111111111111111111111111111111111112"

def get_sol_balance(public_key: str) -> float:
    """Get SOL balance for a wallet"""
    try:
        response = requests.post(
            HELIUS_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [public_key]
            }
        )
        
        if response.status_code == 200:
            result = response.json()
            if "result" in result:
                return float(result["result"]["value"]) / 1e9
    except Exception as e:
        logger.error(f"Failed to get SOL balance: {str(e)}")
    
    return 0

def wait_for_transaction_confirmation(signature: str, max_retries: int = 30) -> bool:
    """Wait for transaction confirmation and verify success"""
    for _ in range(max_retries):
        try:
            response = requests.post(
                HELIUS_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        signature,
                        {"maxSupportedTransactionVersion": 0}
                    ]
                }
            )
            
            if response.status_code == 200:
                result = response.json().get("result")
                if result:
                    if result.get("meta", {}).get("err") is None:
                        return True
                    else:
                        logger.error(f"Transaction failed: {result['meta']['err']}")
                        return False
                        
        except Exception as e:
            logger.warning(f"Error checking transaction status: {str(e)}")
            
        time.sleep(1)
    
    return False

def get_quote(input_mint: str, output_mint: str, amount: str) -> dict:
    """Get optimized quote from Jupiter"""
    try:
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&restrictIntermediateTokens=true"
        response = requests.get(quote_url)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Quote failed: {str(e)}")
        raise

def create_swap_data(quote_response: dict, sender_public_key: str) -> dict:
    """Create optimized swap data according to Jupiter's recommendations"""
    return {
        "quoteResponse": quote_response,
        "userPublicKey": sender_public_key,
        "wrapUnwrapSOL": True,
        "useVersionedTransaction": True,
        "dynamicComputeUnitLimit": True,
        "dynamicSlippage": {
            "maxBps": 300
        },
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": 10000000,
                "priorityLevel": "veryHigh",
                "global": False
            }
        }
    }

def send_transaction(encoded_tx: str) -> str:
    """Send transaction with fallback"""
    try:
        # Try Jupiter endpoint first
        send_response = requests.post(
            "https://worker.jup.ag/send-transaction",
            json={"transaction": encoded_tx},
            headers={"Content-Type": "application/json"}
        )
        
        if send_response.status_code == 200:
            return send_response.json().get("txid")
            
        # Fallback to Helius
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                encoded_tx,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 3
                }
            ]
        }
        
        response = requests.post(
            HELIUS_URL,
            json=rpc_request,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            raise Exception(f"Send failed: {response.text}")
            
        result = response.json()
        if "error" in result:
            raise Exception(f"Send failed: {result}")
            
        return result["result"]
        
    except Exception as e:
        logger.error(f"Failed to send transaction: {str(e)}")
        raise

def buy_tokens(contract_address: str, amount: float, private_key: str):
    try:
        # Initial setup
        private_key_bytes = base58.b58decode(private_key)
        sender_keypair = Keypair.from_bytes(private_key_bytes)
        sender_public_key = str(sender_keypair.pubkey())
        
        # Check balance
        sol_balance = get_sol_balance(sender_public_key)
        if sol_balance < amount:
            raise Exception(f"Insufficient SOL balance. You have {sol_balance} SOL but trying to spend {amount} SOL")
        
        # Get quote
        amount_in_lamports = str(int(amount * (10 ** 9)))
        quote_response = get_quote(SOL_ADDRESS, contract_address, amount_in_lamports)
        
        # Create swap data
        swap_data = create_swap_data(quote_response, sender_public_key)
        
        # Get swap transaction
        swap_response = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json=swap_data
        )
        
        if swap_response.status_code != 200:
            raise Exception(f"Swap transaction failed: {swap_response.text}")
            
        # Sign transaction
        swap_instruction = swap_response.json()["swapTransaction"]
        unsigned_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_instruction))
        
        signature = sender_keypair.sign_message(bytes(unsigned_tx.message))
        signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
        encoded_tx = base64.b64encode(bytes(signed_tx)).decode()
        
        # Send transaction
        tx_signature = send_transaction(encoded_tx)
        print(f"Transaction submitted. Waiting for confirmation...")
        
        # Wait for confirmation
        if wait_for_transaction_confirmation(tx_signature):
            print(f"Transaction confirmed! View details: https://solscan.io/tx/{tx_signature}")
            return tx_signature
        else:
            print("Transaction failed or timed out!")
            return None

    except Exception as e:
        print(f"Error: {str(e)}")
        return None

def sell_tokens(contract_address: str, amount: float, private_key: str):
    try:
        # Get token account and balance
        private_key_bytes = base58.b58decode(private_key)
        sender_keypair = Keypair.from_bytes(private_key_bytes)
        sender_public_key = str(sender_keypair.pubkey())
        
        response = requests.post(
            HELIUS_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    sender_public_key,
                    {"mint": contract_address},
                    {"encoding": "jsonParsed"}
                ]
            }
        )
        
        data = response.json()
        if "result" not in data or not data["result"]["value"]:
            raise Exception("No token account found")
            
        token_account = data["result"]["value"][0]["pubkey"]
        token_decimals = data["result"]["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["decimals"]
        current_balance = float(data["result"]["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) / (10 ** token_decimals)
        
        if amount == 0 or amount > current_balance:
            amount = current_balance
            
        if amount <= 0:
            raise Exception("No tokens to sell")
            
        print(f"Selling {amount} tokens...")
        
        # Check if there's enough SOL for transaction fees
        sol_balance = get_sol_balance(sender_public_key)
        if sol_balance < 0.002:  # Minimum SOL for fees
            raise Exception(f"Insufficient SOL balance for transaction fees. You have {sol_balance} SOL")
        
        # Get quote
        amount_in_smallest_unit = str(int(amount * (10 ** token_decimals)))
        quote_response = get_quote(contract_address, SOL_ADDRESS, amount_in_smallest_unit)
        
        # Create swap data
        swap_data = create_swap_data(quote_response, sender_public_key)
        
        # Get swap transaction
        swap_response = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json=swap_data
        )
        
        if swap_response.status_code != 200:
            raise Exception(f"Swap transaction failed: {swap_response.text}")
            
        # Sign transaction
        swap_instruction = swap_response.json()["swapTransaction"]
        unsigned_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_instruction))
        
        signature = sender_keypair.sign_message(bytes(unsigned_tx.message))
        signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
        encoded_tx = base64.b64encode(bytes(signed_tx)).decode()
        
        # Send transaction
        tx_signature = send_transaction(encoded_tx)
        print(f"Transaction submitted. Waiting for confirmation...")
        
        # Wait for confirmation
        if wait_for_transaction_confirmation(tx_signature):
            print(f"Transaction confirmed! View details: https://solscan.io/tx/{tx_signature}")
            
            # Verify balance change
            time.sleep(2)
            verify_response = requests.post(
                HELIUS_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountBalance",
                    "params": [token_account]
                }
            )
            
            if "result" in verify_response.json():
                new_balance = float(verify_response.json()["result"]["value"]["amount"]) / (10 ** token_decimals)
                if new_balance < current_balance:
                    print(f"Balance reduced from {current_balance} to {new_balance}")
                else:
                    print("Warning: Token balance did not decrease as expected")
            
            return tx_signature
        else:
            print("Transaction failed or timed out!")
            return None

    except Exception as e:
        print(f"Error: {str(e)}")
        return None

def main():
    print("Welcome to Solana Token Trader!")
    private_key = input("Enter your private key: ")
    
    while True:
        print("\n1. Buy tokens")
        print("2. Sell tokens") 
        print("3. Exit")
        
        choice = input("Enter choice (1-3): ")
        
        if choice == "3":
            print("Thank you for using Solana Token Trader!")
            break
            
        if choice not in ["1", "2"]:
            print("Invalid choice")
            continue
            
        contract_address = input("Enter token contract address: ")
        
        if choice == "1":
            try:
                amount = float(input("Enter SOL amount: "))
                print(f"\nInitiating buy of tokens for {amount} SOL...")
                tx_sig = buy_tokens(contract_address, amount, private_key)
                if not tx_sig:
                    print("Transaction failed")
            except ValueError:
                print("Invalid amount")
        else:
            try:
                amount_input = input("Enter token amount (or press Enter for all): ")
                amount = float(amount_input) if amount_input else 0
                print(f"\nInitiating sell of tokens...")
                tx_sig = sell_tokens(contract_address, amount, private_key)
                if not tx_sig:
                    print("Transaction failed")
            except ValueError:
                print("Invalid amount")

if __name__ == "__main__":
    main()
