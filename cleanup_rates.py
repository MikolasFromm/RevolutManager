#!/usr/bin/env python3
"""
Script to clean up the rates database and keep only GBP->GBP and CZK->GBP rates.
This will also modify the CNB rate fetching to only work with GBP.
"""

from app import create_app, db, PredefinedRate, Income, Cost, ExpectedCost

def main():
    app = create_app()
    
    with app.app_context():
        print("Current rates in database:")
        rates = PredefinedRate.query.all()
        for rate in rates:
            print(f"  ID {rate.id}: {rate.from_currency}->{rate.to_currency} = {rate.rate} (desc: {rate.description})")
        
        print("\nUsage analysis:")
        for rate in rates:
            income_count = Income.query.filter_by(rate_id=rate.id).count()
            cost_count = Cost.query.filter_by(rate_id=rate.id).count()
            expected_count = ExpectedCost.query.filter_by(rate_id=rate.id).count()
            total_usage = income_count + cost_count + expected_count
            print(f"  Rate ID {rate.id} ({rate.from_currency}->{rate.to_currency}): {total_usage} total uses ({income_count} income, {cost_count} cost, {expected_count} expected)")
        
        # Find rates we want to keep: GBP->GBP and CZK->GBP
        gbp_gbp = PredefinedRate.query.filter_by(from_currency='GBP', to_currency='GBP').first()
        czk_gbp = PredefinedRate.query.filter_by(from_currency='CZK', to_currency='GBP').first()
        
        print(f"\nRates to keep:")
        if gbp_gbp:
            print(f"  GBP->GBP (ID {gbp_gbp.id}): {gbp_gbp.rate}")
        else:
            print("  GBP->GBP: NOT FOUND - will create")
            
        if czk_gbp:
            print(f"  CZK->GBP (ID {czk_gbp.id}): {czk_gbp.rate}")
        else:
            print("  CZK->GBP: NOT FOUND - will create")
        
        # Ask for confirmation before proceeding
        response = input("\nDo you want to proceed with cleanup? This will:\n1. Delete all other rates\n2. Create missing GBP->GBP and CZK->GBP rates\n3. Update entries to use the kept rates\n(y/N): ")
        
        if response.lower() != 'y':
            print("Cleanup cancelled.")
            return
        
        # Create missing rates if needed
        if not gbp_gbp:
            gbp_gbp = PredefinedRate(from_currency='GBP', to_currency='GBP', rate=1.0, description=None)
            db.session.add(gbp_gbp)
            db.session.flush()  # Get the ID
            print(f"Created GBP->GBP rate with ID {gbp_gbp.id}")
        
        if not czk_gbp:
            # We'll need to fetch the current rate from CNB or use a default
            czk_gbp = PredefinedRate(from_currency='CZK', to_currency='GBP', rate=0.035, description=None)  # Approximate rate
            db.session.add(czk_gbp)
            db.session.flush()  # Get the ID
            print(f"Created CZK->GBP rate with ID {czk_gbp.id} (rate: {czk_gbp.rate})")
        
        # Update all entries to use appropriate rates
        print("\nUpdating entries to use correct rates...")
        
        # Update incomes
        incomes_updated = 0
        for income in Income.query.all():
            old_rate_id = income.rate_id
            if income.currency == 'GBP':
                income.rate_id = gbp_gbp.id
                income.norm_amount = income.amount * gbp_gbp.rate  # Should be same amount
            elif income.currency == 'CZK':
                income.rate_id = czk_gbp.id
                income.norm_amount = income.amount * czk_gbp.rate
            else:
                print(f"  Warning: Income ID {income.id} uses unsupported currency {income.currency}")
                continue
            
            if old_rate_id != income.rate_id:
                incomes_updated += 1
        
        # Update costs
        costs_updated = 0
        for cost in Cost.query.all():
            old_rate_id = cost.rate_id
            if cost.currency == 'GBP':
                cost.rate_id = gbp_gbp.id
                cost.norm_amount = cost.amount * gbp_gbp.rate
            elif cost.currency == 'CZK':
                cost.rate_id = czk_gbp.id
                cost.norm_amount = cost.amount * czk_gbp.rate
            else:
                print(f"  Warning: Cost ID {cost.id} uses unsupported currency {cost.currency}")
                continue
            
            if old_rate_id != cost.rate_id:
                costs_updated += 1
        
        # Update expected costs
        expected_updated = 0
        for expected in ExpectedCost.query.all():
            old_rate_id = expected.rate_id
            if expected.currency == 'GBP':
                expected.rate_id = gbp_gbp.id
                expected.norm_amount = expected.amount * gbp_gbp.rate
                expected.norm_remaining = expected.amount * gbp_gbp.rate  # Reset remaining
            elif expected.currency == 'CZK':
                expected.rate_id = czk_gbp.id
                expected.norm_amount = expected.amount * czk_gbp.rate
                expected.norm_remaining = expected.amount * czk_gbp.rate  # Reset remaining
            else:
                print(f"  Warning: Expected cost ID {expected.id} uses unsupported currency {expected.currency}")
                continue
            
            if old_rate_id != expected.rate_id:
                expected_updated += 1
        
        print(f"Updated {incomes_updated} incomes, {costs_updated} costs, {expected_updated} expected costs")
        
        # Now delete all other rates
        rates_to_delete = PredefinedRate.query.filter(
            ~PredefinedRate.id.in_([gbp_gbp.id, czk_gbp.id])
        ).all()
        
        print(f"\nDeleting {len(rates_to_delete)} unused rates...")
        for rate in rates_to_delete:
            print(f"  Deleting {rate.from_currency}->{rate.to_currency} (ID {rate.id})")
            db.session.delete(rate)
        
        # Commit all changes
        db.session.commit()
        print(f"\nCleanup completed successfully!")
        print(f"Remaining rates:")
        remaining_rates = PredefinedRate.query.all()
        for rate in remaining_rates:
            print(f"  ID {rate.id}: {rate.from_currency}->{rate.to_currency} = {rate.rate}")


def delete_all_rates():
    app = create_app()
    
    with app.app_context():
        print("This will delete ALL rates from the database.")
        response = input("Are you sure you want to proceed? (y/N): ")
        
        if response.lower() != 'y':
            print("Operation cancelled.")
            return
        
        rates = PredefinedRate.query.all()
        print(f"Deleting {len(rates)} rates...")
        for rate in rates:
            print(f"  Deleting {rate.from_currency}->{rate.to_currency} (ID {rate.id})")
            db.session.delete(rate)
        
        db.session.commit()
        print("All rates deleted.")


if __name__ == '__main__':
    print("Choose an option:")
    print("1. Delete all rates")
    print("2. Cleanup rates")
    choice = input("Enter your choice (1/2): ")
    if choice == '1':
        delete_all_rates()
    elif choice == '2':
        main()
    else:
        print("Invalid choice. Exiting.")