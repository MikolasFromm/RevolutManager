from flask import Flask, render_template, redirect, url_for, request, flash, Blueprint
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import calendar
import requests
import re
from typing import Optional, Dict, Any

BASE_CURRENCY = 'GBP'
HOME_CURRENCY = 'CZK'

# Use an uninitialized SQLAlchemy object. We'll initialize it in create_app so tests
# can override configuration (like SQLALCHEMY_DATABASE_URI) before DB init.
db = SQLAlchemy()

## Norm_amount is amount converted to base currency (GBP)

## Norm_rate is the rate used for that conversion

class PredefinedRate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    from_currency = db.Column(db.String(10), nullable=False)
    to_currency = db.Column(db.String(10), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(256), nullable=True)  # Added description field
    created = db.Column(db.DateTime, default=datetime.utcnow)
    updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Ensure unique combination of currency pairs and description
    __table_args__ = (db.UniqueConstraint('from_currency', 'to_currency', 'description'),)
    
    def __repr__(self):
        desc_part = f" ({self.description})" if self.description else ""
        return f"{self.from_currency}-{self.to_currency} ({self.rate}){desc_part}"


class ExpectedCost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(256))
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default=BASE_CURRENCY)
    rate_id = db.Column(db.Integer, db.ForeignKey('predefined_rate.id'), nullable=False)
    norm_amount = db.Column(db.Float, nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    # remaining amount that can be cut from this expected cost
    norm_remaining = db.Column(db.Float, nullable=False)
    
    rate = db.relationship('PredefinedRate', backref=db.backref('expected_costs', lazy=True))
    
    @property
    def current_rate_value(self):
        """Get the current rate value (dynamic for expected costs)"""
        return self.rate.rate
    
    @property
    def current_norm_amount(self):
        """Calculate current normalized amount using live rate"""
        return self.amount * self.current_rate_value
    
    @property
    def current_norm_remaining(self):
        """Calculate current normalized remaining using live rate"""
        # Calculate what percentage of the original amount remains
        if self.norm_amount <= 0:
            return 0
        remaining_ratio = self.norm_remaining / self.norm_amount
        return self.current_norm_amount * remaining_ratio


class Income(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.String(256))
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False)
    rate_id = db.Column(db.Integer, db.ForeignKey('predefined_rate.id'), nullable=False)
    norm_amount = db.Column(db.Float, nullable=False)
    # for historytical reference, store the rate used at the time of entry
    norm_rate = db.Column(db.Float, nullable=True)
    # whether the rate is fixed (True) or should use current rate (False)
    fixed_rate = db.Column(db.Boolean, nullable=False, default=True)
    
    rate = db.relationship('PredefinedRate', backref=db.backref('incomes', lazy=True))
    
    @property
    def current_norm_amount(self):
        """Calculate current normalized amount - fixed rate uses historical rate, dynamic uses current rate"""
        if self.fixed_rate:
            return self.norm_amount  # Use stored normalized amount
        else:
            return self.amount * self.rate.rate  # Use current rate


class Cost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.String(256))
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default=BASE_CURRENCY)
    rate_id = db.Column(db.Integer, db.ForeignKey('predefined_rate.id'), nullable=False)
    norm_amount = db.Column(db.Float, nullable=False)
    # for historytical reference, store the rate used at the time of entry
    norm_rate = db.Column(db.Float, nullable=True)
    expected_ref_id = db.Column(db.Integer, db.ForeignKey('expected_cost.id'), nullable=True)
    
    rate = db.relationship('PredefinedRate', backref=db.backref('costs', lazy=True))
    expected_ref = db.relationship('ExpectedCost', backref=db.backref('real_costs', lazy=True))


class MonthlyCostTarget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    target_amount = db.Column(db.Float, nullable=False)  # In base currency (GBP)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<MonthlyCostTarget: {self.target_amount} {BASE_CURRENCY}>"

### We'll register the API routes inside create_app so the DB is initialized first.

def fetch_cnb_rates():
    """Fetch current currency rates from Czech National Bank"""
    try:
        url = "https://www.cnb.cz/cs/financni_trhy/devizovy_trh/kurzy_devizoveho_trhu/denni_kurz.txt"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # Parse the text file to find currency rates
        content = response.text
        lines = content.split('\n')
        
        from_currency = {}
        
        cnb_curr = 'CZK'
        for line in lines[2:]:
            # Format: země|měna|množství|kód|kurz
            # Example: Velká Británie|libra|1|GBP|28,146
            parts = line.split('|')
            if len(parts) >= 5:
                # Czech format uses comma as decimal separator
                rate_str = parts[4].strip().replace(',', '.')
                quantity = int(parts[2].strip()) if parts[2].strip().isdigit() else 1
                target_currency = parts[3].strip()
                rate = float(rate_str) / quantity
                from_currency[target_currency] = rate
        
        rates = {}
        
        ## generate crossrates with CZK in the middle
        for curr1, rate1 in from_currency.items():
            ## curr1 -> CZK
            for curr2, rate2 in from_currency.items():
                ## curr2 -> CZK
                if curr1 != curr2:
                    ## curr1 -> CZK -> curr2
                    rate2_flip = 1 / rate2
                    rate = rate1 * rate2_flip
                    rates[f"{curr1}_TO_{curr2}"] = rate
                    rates[f"{curr2}_TO_{curr1}"] = 1.0 / rate

        ## generate exact rates
        for curr, rate in from_currency.items():
            rates[f"{curr}_TO_CZK"] = rate
            rates[f"CZK_TO_{curr}"] = 1.0 / rate

        print(f"Fetched CNB rates: {len(rates)}")
        return rates
    
    except Exception as e:
        print(f"Error fetching CNB rates: {e}")
        return {}

def update_cnb_rates(app):
    """Update currency rates from CNB"""
    try:
        with app.app_context():
            # Fetch current rates
            cnb_rates = fetch_cnb_rates()
            if not cnb_rates or 'CZK_TO_GBP' not in cnb_rates or 'GBP_TO_CZK' not in cnb_rates:
                print("Could not fetch GBP rate from CNB, keeping existing rate")
                return
            
            now = datetime.utcnow()
            
            ## iterate over each rate to BASE_CURRENCY
            cnb_rates_to_base = {k: v for k, v in cnb_rates.items() if k.endswith(f"_TO_{BASE_CURRENCY}") or k.endswith(f"_TO_{HOME_CURRENCY}")}
            for rate_key, rate_value in cnb_rates_to_base.items():
                parts = rate_key.split('_TO_')
                if len(parts) != 2:
                    continue
                from_curr = parts[0]
                to_curr = parts[1]
                
                if from_curr == to_curr:
                    continue
                
                # Update or create the rate without description
                rate = PredefinedRate.query.filter_by(
                    from_currency=from_curr, 
                    to_currency=to_curr, 
                    description=None
                ).first()
                
                if rate and rate.rate != rate_value:
                    rate.rate = rate_value
                    rate.updated = now
                    print(f"Updated {from_curr}->{to_curr} rate to {rate_value:.6f}")
                elif not rate:
                    rate = PredefinedRate(
                        from_currency=from_curr, 
                        to_currency=to_curr, 
                        rate=rate_value,
                        description=None
                    )
                    db.session.add(rate)
                    print(f"Created {from_curr}->{to_curr} rate: {rate_value:.6f}")
            
            db.session.commit()
            print("Successfully updated CNB rates")
    
    except Exception as e:
        print(f"Error updating CNB rates: {e}")
        if 'app' in locals():
            db.session.rollback()


def create_app(test_config: Optional[Dict[str, Any]] = None):
    """Create and configure the Flask app. If test_config is provided it will update
    the default configuration (useful for tests to set SQLALCHEMY_DATABASE_URI).
    """
    app = Flask(__name__)
    # default configuration
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///revolut.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'dev-secret'

    if test_config:
        app.config.update(test_config)

    # initialize DB with this app
    db.init_app(app)

    bp = Blueprint('api', __name__)


    @bp.route('/rates', methods=['GET'])
    def list_rates():
        # Only return official CNB rates (no description)
        rates = PredefinedRate.query.filter_by(description=None).order_by(PredefinedRate.from_currency, PredefinedRate.to_currency).all()
        return {"rates": [
            {
                "id": r.id, 
                "from_currency": r.from_currency, 
                "to_currency": r.to_currency, 
                "rate": r.rate
            } for r in rates
        ]}

    @bp.route('/rates/update-cnb', methods=['POST'])
    def update_cnb_rates_endpoint():
        """Manually update currency rates from Czech National Bank"""
        try:
            update_cnb_rates(app)
            return {"success": True, "message": "Rates updated from CNB"}
        except Exception as e:
            return {"success": False, "error": str(e)}, 500

    @bp.route('/balance', methods=['GET'])
    def get_balance():
        # For incomes, we need to handle fixed vs dynamic rates
        incomes = Income.query.all()
        total_income = sum(i.current_norm_amount for i in incomes)
        
        total_costs = db.session.query(db.func.coalesce(db.func.sum(Cost.norm_amount), 0.0)).scalar()
        
        # For expected costs, calculate using current rates (dynamic)
        expected_costs = ExpectedCost.query.all()
        total_expected_remaining = sum(e.current_norm_remaining for e in expected_costs)
        
        total_income = float(total_income or 0.0)
        total_costs = float(total_costs or 0.0)
        total_expected_remaining = float(total_expected_remaining or 0.0)
        balance = total_income - (total_costs + total_expected_remaining)
        
        # norm is GBP, recalculate to CZK for display as well
        ## get the rate
        db_rate = PredefinedRate.query.filter_by(from_currency=BASE_CURRENCY, to_currency='CZK').first()
        if db_rate:
            czk_rate = db_rate.rate
            balance_czk = balance * czk_rate
            total_income_czk = total_income * czk_rate
            total_costs_czk = total_costs * czk_rate
            total_expected_remaining_czk = total_expected_remaining * czk_rate
        
        return {
            "balance": balance,
            "total_income": total_income,
            "total_costs": total_costs,
            "total_expected_remaining": total_expected_remaining,
            "balance_czk": balance_czk if db_rate else None,
            "total_income_czk": total_income_czk if db_rate else None,
            "total_costs_czk": total_costs_czk if db_rate else None,
            "total_expected_remaining_czk": total_expected_remaining_czk if db_rate else None,
        }
    
    def get_monthly_summary(months_back=3):
        """Get monthly balance summary for the last X months"""
        summaries = []
        now = datetime.now()
        
        for i in range(months_back):
            # Calculate the target month
            year = now.year
            month = now.month - i
            
            # Handle year rollover
            while month <= 0:
                month += 12
                year -= 1
            
            # Get first and last day of the month
            first_day = datetime(year, month, 1)
            if month == 12:
                last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = datetime(year, month + 1, 1) - timedelta(days=1)
            
            # Query incomes for this month
            monthly_incomes = Income.query.filter(
                Income.date >= first_day,
                Income.date <= last_day
            ).all()
            
            # Query costs for this month
            monthly_costs = Cost.query.filter(
                Cost.date >= first_day,
                Cost.date <= last_day
            ).all()
            
            # Calculate totals
            total_income = sum(income.current_norm_amount for income in monthly_incomes)
            total_costs = sum(cost.norm_amount for cost in monthly_costs)
            balance = total_income - total_costs
            
            # Get the single monthly cost target (if it exists)
            cost_target = MonthlyCostTarget.query.first()
            target_amount = cost_target.target_amount if cost_target else None
            cost_diff = (target_amount - total_costs) if target_amount is not None else None
            
            summaries.append({
                'year': year,
                'month': month,
                'month_name': calendar.month_name[month],
                'income': float(total_income),
                'costs': float(total_costs),
                'balance': float(balance),
                'income_count': len(monthly_incomes),
                'costs_count': len(monthly_costs),
                'target_costs': float(target_amount) if target_amount is not None else None,
                'cost_diff': float(cost_diff) if cost_diff is not None else None
            })
        
        return summaries

    @bp.route('/monthly-summary', methods=['GET'])
    def get_monthly_summary_api():
        """API endpoint for monthly balance summary"""
        months = request.args.get('months', 3, type=int)
        if months < 1 or months > 24:
            return {"error": "months must be between 1 and 24"}, 400
        
        summaries = get_monthly_summary(months)
        
        # Add CZK conversion
        db_rate = PredefinedRate.query.filter_by(from_currency=BASE_CURRENCY, to_currency='CZK').first()
        czk_rate = db_rate.rate if db_rate else None
        
        if czk_rate:
            for summary in summaries:
                summary['income_czk'] = summary['income'] * czk_rate
                summary['costs_czk'] = summary['costs'] * czk_rate
                summary['balance_czk'] = summary['balance'] * czk_rate
                if summary['target_costs'] is not None:
                    summary['target_costs_czk'] = summary['target_costs'] * czk_rate
                if summary['cost_diff'] is not None:
                    summary['cost_diff_czk'] = summary['cost_diff'] * czk_rate
        
        return {"summaries": summaries, "has_czk": czk_rate is not None}

    @bp.route('/income', methods=['POST'])
    def add_income():
        data = request.get_json() or {}
        required = ['amount', 'rate_id', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, rate_id, description"}, 400
        
        amount = float(data['amount'])
        rate_id = int(data['rate_id'])
        desc = data['description']
        fixed_rate = data.get('fixed_rate', True)  # Default to True (fixed rate)
        
        # Handle date - default to current datetime if not provided
        date_value = datetime.utcnow()
        if 'date' in data and data['date']:
            try:
                date_value = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        # Get the rate
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
        
        # Currency is deduced from rate
        currency = rate.from_currency
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        income = Income(description=desc, 
                        amount=amount,
                        currency=currency, 
                        rate_id=rate_id,
                        norm_rate=rate_value,  # Store historical rate
                        norm_amount=norm_amount,
                        fixed_rate=fixed_rate,
                        date=date_value)
        db.session.add(income)
        db.session.commit()
        return {"id": income.id, "norm_amount": norm_amount, "currency": currency, "fixed_rate": fixed_rate}

    @bp.route('/income', methods=['GET'])
    def list_incomes():
        incomes = Income.query.order_by(Income.date.desc()).all()
        return {"incomes": [
            {
                "id": i.id, 
                "date": i.date.isoformat(), 
                "description": i.description, 
                "amount": i.amount, 
                "currency": i.currency, 
                "rate": i.norm_rate,
                "rate_id": i.rate_id,
                "rate_name": f"{i.rate.from_currency}-{i.rate.to_currency}" if i.rate else "legacy",
                "fixed_rate": i.fixed_rate,
                "current_norm_amount": i.current_norm_amount
            } for i in incomes
        ]}

    @bp.route('/income/<int:income_id>', methods=['PUT'])
    def update_income(income_id):
        income = db.session.get(Income, income_id)
        if income is None:
            return {"error": "income not found"}, 404
        
        data = request.get_json() or {}
        
        # Update description if provided
        if 'description' in data:
            income.description = data['description']
        
        # Update amount if provided
        if 'amount' in data:
            new_amount = float(data['amount'])
            if new_amount <= 0:
                return {"error": "amount must be positive"}, 400
            income.amount = new_amount
            income.norm_amount = new_amount * income.norm_rate
        
        # Update date if provided
        if 'date' in data:
            try:
                income.date = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        db.session.commit()
        return {"id": income.id, "amount": income.amount, "norm_amount": income.norm_amount, "description": income.description}

    @bp.route('/cost', methods=['POST'])
    def add_cost():
        data = request.get_json() or {}
        required = ['amount', 'rate_id', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, rate_id, description"}, 400
        
        amount = float(data['amount'])
        rate_id = int(data['rate_id'])
        desc = data['description']
        expected_ref_id = data.get('expected_ref_id') or None
        
        # Handle date - default to current datetime if not provided
        date_value = datetime.utcnow()
        if 'date' in data and data['date']:
            try:
                date_value = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        # Get the rate
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
        
        # Currency is deduced from rate
        currency = rate.from_currency
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        cost = Cost(description=desc, 
                    amount=amount,
                    currency=currency,
                    rate_id=rate_id,
                    norm_rate=rate_value,  # Store historical rate
                    norm_amount=norm_amount,
                    expected_ref_id=expected_ref_id,
                    date=date_value)
        db.session.add(cost)
        db.session.commit()
        return {"id": cost.id, "currency": currency}

    @bp.route('/cost', methods=['GET'])
    def list_costs():
        costs = Cost.query.order_by(Cost.date.desc()).all()
        return {"costs": [
            {
                "id": c.id, 
                "date": c.date.isoformat(), 
                "description": c.description, 
                "amount": c.amount, 
                "expected_ref_id": c.expected_ref_id, 
                "currency": c.currency, 
                "rate": c.norm_rate,
                "rate_id": c.rate_id,
                "rate_name": f"{c.rate.from_currency}-{c.rate.to_currency}" if c.rate else "legacy"
            } for c in costs
        ]}

    @bp.route('/cost/<int:cost_id>', methods=['PUT'])
    def update_cost(cost_id):
        cost = db.session.get(Cost, cost_id)
        if cost is None:
            return {"error": "cost not found"}, 404
        
        data = request.get_json() or {}
        
        # Update description if provided
        if 'description' in data:
            cost.description = data['description']
        
        # Update amount if provided
        if 'amount' in data:
            new_amount = float(data['amount'])
            if new_amount <= 0:
                return {"error": "amount must be positive"}, 400
            cost.amount = new_amount
            cost.norm_amount = new_amount * cost.norm_rate
        
        # Update date if provided
        if 'date' in data:
            try:
                cost.date = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        db.session.commit()
        return {"id": cost.id, "amount": cost.amount, "norm_amount": cost.norm_amount, "description": cost.description, "currency": cost.currency, "rate_id": cost.rate_id}

    @bp.route('/expected', methods=['GET', 'POST'])
    def expected_list_or_create():
        if request.method == 'GET':
            expected = ExpectedCost.query.all()
            return {"expected": [
                {
                    "id": e.id, 
                    "created": e.created.isoformat(),
                    "description": e.description, 
                    "amount": e.amount, 
                    "remaining": e.current_norm_remaining, 
                    "currency": e.currency, 
                    "rate": e.current_rate_value,
                    "rate_id": e.rate_id,
                    "rate_name": f"{e.rate.from_currency}-{e.rate.to_currency}" if e.rate else "legacy"
                } for e in expected
            ]}
        
        data = request.get_json() or {}
        required = ['amount', 'rate_id', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, rate_id, description"}, 400
        
        amount = float(data['amount'])
        rate_id = int(data['rate_id'])
        desc = data['description']
        
        # Get the rate
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
        
        # Currency is deduced from rate
        currency = rate.from_currency
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        e = ExpectedCost(description=desc, 
                         amount=amount, 
                         currency=currency, 
                         rate_id=rate_id,
                         norm_amount=norm_amount,
                         norm_remaining=norm_amount)
        db.session.add(e)
        db.session.commit()
        return {"id": e.id, "currency": currency}

    @bp.route('/expected/<int:expected_id>', methods=['PUT'])
    def update_expected(expected_id):
        expected = db.session.get(ExpectedCost, expected_id)
        if expected is None:
            return {"error": "expected cost not found"}, 404
        
        data = request.get_json() or {}
        
        # Update description if provided
        if 'description' in data:
            expected.description = data['description']
        
        # Handle amount and/or rate updates
        original_norm_amount = expected.norm_amount
        original_norm_remaining = expected.norm_remaining
        original_norm_paid = original_norm_amount - original_norm_remaining
        
        # Update amount if provided
        if 'amount' in data:
            new_amount = float(data['amount'])
            if new_amount <= 0:
                return {"error": "amount must be positive"}, 400
            expected.amount = new_amount
            
        # Recalculate norm_amount and norm_remaining
        # For expected costs, we need to preserve the remaining ratio
        # but update based on current rate (dynamic)
        expected.norm_amount = expected.amount * expected.rate.rate
        expected.norm_remaining = expected.norm_amount - original_norm_paid ## what was paid is not changed
        
        db.session.commit()
        return {"id": expected.id, "amount": expected.amount, "norm_amount": expected.norm_amount, 
                "norm_remaining": expected.norm_remaining, "description": expected.description, 
                "currency": expected.currency, "rate_id": expected.rate_id}

    @bp.route('/income/<int:income_id>', methods=['DELETE'])
    def delete_income(income_id):
        income = db.session.get(Income, income_id)
        if income is None:
            return {"error": "income not found"}, 404
        db.session.delete(income)
        db.session.commit()
        return {"deleted": income_id}

    @bp.route('/expected/<int:expected_id>/cut', methods=['POST'])
    def cut_from_expected(expected_id):
        # find expected
        expected = ExpectedCost.query.get(expected_id)
        if expected is None:
            return {"error": "expected not found"}, 404

        payload = request.get_json() or {}
        if 'amount' not in payload:
            return {"error": "missing amount"}, 400
        amount = float(payload['amount'])
        if amount <= 0:
            return {"error": "amount must be positive"}, 400
        
        # Check against current remaining amount
        current_remaining = expected.current_norm_remaining
        if amount > current_remaining:
            return {"error": f"amount {amount} exceeds current remaining {current_remaining:.2f}"}, 400

        # reduce remaining (using original norm_amount scale)
        reduction_ratio = amount / expected.current_norm_amount if expected.current_norm_amount > 0 else 0
        expected.norm_remaining -= expected.norm_amount * reduction_ratio

        # Create cost record - use same rate as expected cost
        rate_id = payload.get('rate_id') or expected.rate_id
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
        
        currency = rate.from_currency
        rate_value = rate.rate
        norm_amount = amount * rate_value
        desc = payload.get('description') or f"cut from expected {expected.description}"
        
        cost = Cost(description=desc, 
                    amount=amount,
                    currency=currency,
                    rate_id=rate_id,
                    norm_rate=rate_value,  # Store historical rate
                    norm_amount=norm_amount,
                    expected_ref_id=expected.id)
        
        db.session.add(cost)
        db.session.commit()
        return {"cost_id": cost.id, "expected_remaining": expected.current_norm_remaining}

    @bp.route('/cost/<int:cost_id>', methods=['DELETE'])
    def delete_cost(cost_id):
        cost = db.session.get(Cost, cost_id)
        if cost is None:
            return {"error": "cost not found"}, 404
        if cost.expected_ref is not None:
            cost.expected_ref.norm_remaining += cost.norm_amount
        db.session.delete(cost)
        db.session.commit()
        return {"deleted": cost_id}

    @bp.route('/expected/<int:expected_id>', methods=['DELETE'])
    def delete_expected(expected_id):
        expected = db.session.get(ExpectedCost, expected_id)
        if expected is None:
            return {"error": "expected not found"}, 404
        # orphan related costs rather than deleting historical costs
        related = Cost.query.filter_by(expected_ref_id=expected_id).all()
        for c in related:
            c.expected_ref_id = None
        db.session.delete(expected)
        db.session.commit()
        return {"deleted": expected_id}

    # New currency-based endpoints for simplified UI
    @bp.route('/currencies', methods=['GET'])
    def list_currencies():
        """Return available currencies for the simplified UI based on official CNB rates only"""
        # Get all unique currencies that have official CNB rates to GBP (no description = official)
        rates = PredefinedRate.query.filter_by(to_currency=BASE_CURRENCY, description=None).all()
        
        currencies = []
        
        for rate in rates:
            currency_code = rate.from_currency
            if currency_code not in [c['code'] for c in currencies]:
                currencies.append({
                    'code': currency_code,
                    'rate': rate.rate
                })
        
        return {"currencies": currencies}

    @bp.route('/income/currency', methods=['POST'])
    def add_income_currency():
        """Add income using currency and rate type instead of rate_id"""
        data = request.get_json() or {}
        required = ['amount', 'currency', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, currency, description"}, 400
        
        amount = float(data['amount'])
        currency = data['currency'].upper()
        desc = data['description']
        fixed_rate = data.get('fixed_rate', True)  # Default to True (fixed rate)
        
        # Handle date - default to current datetime if not provided
        date_value = datetime.utcnow()
        if 'date' in data and data['date']:
            try:
                date_value = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        # Find appropriate official CNB rate for the currency to GBP
        rate = PredefinedRate.query.filter_by(from_currency=currency, to_currency=BASE_CURRENCY, description=None).first()
        
        if rate is None:
            return {"error": f"no official rate found for {currency}"}, 404
        
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        income = Income(description=desc, 
                        amount=amount,
                        currency=currency, 
                        rate_id=rate.id,
                        norm_rate=rate_value,  # Store historical rate
                        norm_amount=norm_amount,
                        fixed_rate=fixed_rate,
                        date=date_value)
        db.session.add(income)
        db.session.commit()
        return {"id": income.id, "norm_amount": norm_amount, "currency": currency, "fixed_rate": fixed_rate}

    @bp.route('/cost/currency', methods=['POST'])
    def add_cost_currency():
        """Add cost using currency instead of rate_id"""
        data = request.get_json() or {}
        required = ['amount', 'currency', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, currency, description"}, 400
        
        amount = float(data['amount'])
        currency = data['currency'].upper()
        desc = data['description']
        expected_ref_id = data.get('expected_ref_id') or None
        
        # Handle date - default to current datetime if not provided
        date_value = datetime.utcnow()
        if 'date' in data and data['date']:
            try:
                date_value = datetime.fromisoformat(data['date'].replace('Z', '+00:00'))
            except ValueError:
                return {"error": "invalid date format, use ISO format"}, 400
        
        # Find appropriate official CNB rate for the currency to GBP
        rate = PredefinedRate.query.filter_by(from_currency=currency, to_currency=BASE_CURRENCY, description=None).first()
        
        if rate is None:
            return {"error": f"no official rate found for {currency}"}, 404
        
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        cost = Cost(description=desc, 
                    amount=amount,
                    currency=currency,
                    rate_id=rate.id,
                    norm_rate=rate_value,  # Store historical rate
                    norm_amount=norm_amount,
                    expected_ref_id=expected_ref_id,
                    date=date_value)
        db.session.add(cost)
        db.session.commit()
        return {"id": cost.id, "currency": currency}

    @bp.route('/expected/currency', methods=['POST'])
    def add_expected_currency():
        """Add expected cost using currency instead of rate_id"""
        data = request.get_json() or {}
        required = ['amount', 'currency', 'description']
        if not all(k in data for k in required):
            return {"error": "missing required fields: amount, currency, description"}, 400
        
        amount = float(data['amount'])
        currency = data['currency'].upper()
        desc = data['description']
        
        # Find appropriate official CNB rate for the currency to GBP
        rate = PredefinedRate.query.filter_by(from_currency=currency, to_currency=BASE_CURRENCY, description=None).first()
        
        if rate is None:
            return {"error": f"no official rate found for {currency}"}, 404
        
        rate_value = rate.rate
        norm_amount = amount * rate_value
        
        expected = ExpectedCost(description=desc, 
                               amount=amount,
                               currency=currency,
                               rate_id=rate.id,
                               norm_amount=norm_amount,
                               norm_remaining=norm_amount)
        db.session.add(expected)
        db.session.commit()
        return {"id": expected.id, "currency": currency}

    @bp.route('/rates/update-cnb', methods=['POST'])
    def update_rates_from_cnb():
        """Manually trigger CNB rate update"""
        try:
            update_cnb_rates(app)
            return {"success": True, "message": "Rates updated from CNB"}
        except Exception as e:
            return {"error": f"Failed to update rates: {str(e)}"}, 500

    @bp.route('/monthly-target', methods=['GET', 'POST', 'DELETE'])
    def monthly_cost_target():
        """Manage the single monthly cost target"""
        if request.method == 'GET':
            target = MonthlyCostTarget.query.first()
            if target:
                return {"target_amount": target.target_amount}
            else:
                return {"target_amount": None}
        
        elif request.method == 'POST':
            data = request.get_json() or {}
            if 'target_amount' not in data:
                return {"error": "missing required field: target_amount"}, 400
            
            try:
                target_amount = float(data['target_amount'])
            except ValueError:
                return {"error": "target_amount must be a number"}, 400
            
            # Update existing or create new target
            target = MonthlyCostTarget.query.first()
            if target:
                target.target_amount = target_amount
                target.updated = datetime.utcnow()
            else:
                target = MonthlyCostTarget(target_amount=target_amount)
                db.session.add(target)
            
            db.session.commit()
            return {"target_amount": target.target_amount}
        
        elif request.method == 'DELETE':
            target = MonthlyCostTarget.query.first()
            if target:
                db.session.delete(target)
                db.session.commit()
            return {"success": True, "message": "Target deleted"}

    app.register_blueprint(bp)

    # UI routes for a simple friendly interface (local access over VPN)
    @app.route('/')
    def index():
        # Update currency rates from CNB when loading index page
        update_cnb_rates(app)
        
        # server renders the shell; the page will fetch dynamic data via the API endpoints
        return render_template('index.html')

    return app

if __name__ == '__main__':
    # create the real app and run
    real_app = create_app()
    with real_app.app_context():
        db.create_all()
        # Seed default rates if missing
        with real_app.app_context():
            # Add GBP->GBP rate
            if not PredefinedRate.query.filter_by(from_currency=BASE_CURRENCY, to_currency=BASE_CURRENCY).first():
                db.session.add(PredefinedRate(from_currency=BASE_CURRENCY, to_currency=BASE_CURRENCY, rate=1.0, description=None))
            
            # Add CZK->GBP rate (will be updated from CNB)
            if not PredefinedRate.query.filter_by(from_currency='CZK', to_currency=BASE_CURRENCY).first():
                db.session.add(PredefinedRate(from_currency='CZK', to_currency=BASE_CURRENCY, rate=0.035, description=None))
            
            db.session.commit()
    real_app.run(host='0.0.0.0', port=5000, debug=False)
