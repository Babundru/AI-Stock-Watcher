import datetime
from notifier import Notifier

def simulate():
    print("Running Simulation Mode (No API Keys required)...")
    
    # 1. Mock Data
    company = "Tesla"
    article = {
        'title': "Tesla announces breakthrough in battery tech, stock soars",
        'description': "Tesla's new solid-state battery promises 2x range at half the cost.",
        'content': "PALO ALTO, Calif. -- Tesla Inc. today announced a revolutionary breakthrough in solid-state battery technology. The new 4680-Z cells are expected to double the range of Model S and Model X vehicles while reducing manufacturing costs by 50%. Analysts predict this could be a 'game changer' for the EV industry, effectively neutralizing competition from legacy automakers for the next decade. 'This is the moment we've been working towards,' said Elon Musk in a press conference.",
        'publishedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'url': "http://example.com"
    }
    
    # 2. Mock Analysis
    analysis = {
        'sentiment': 'POSITIVE',
        'impact': 'CRITICAL',
        'explanation': 'Major technological advantage gained.',
        'prediction': 'GAP UP (if market closed) / RALLY (if open)'
    }
    
    # 3. Test Notifier
    notifier = Notifier()
    is_open = notifier.is_market_open()
    status = "OPEN" if is_open else "CLOSED"
    print(f"Current Market Status: {status}")
    
    print("\nSimulating incoming news...")
    notifier.notify(company, article, analysis)
    
    print("\nSimulation Complete.")

if __name__ == "__main__":
    simulate()
