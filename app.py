from flask import Flask, render_template, redirect, url_for, request, flash, Blueprint
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import requests
import re
from typing import Optional, Dict, Any

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
    currency = db.Column(db.String(10), nullable=False, default='GBP')
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
    
    rate = db.relationship('PredefinedRate', backref=db.backref('incomes', lazy=True))


class Cost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.String(256))
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default='GBP')
    rate_id = db.Column(db.Integer, db.ForeignKey('predefined_rate.id'), nullable=False)
    norm_amount = db.Column(db.Float, nullable=False)
    # for historytical reference, store the rate used at the time of entry
    norm_rate = db.Column(db.Float, nullable=True)
    expected_ref_id = db.Column(db.Integer, db.ForeignKey('expected_cost.id'), nullable=True)
    
    rate = db.relationship('PredefinedRate', backref=db.backref('costs', lazy=True))
    expected_ref = db.relationship('ExpectedCost', backref=db.backref('real_costs', lazy=True))
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
        
        rates = {}
        target_currencies = ['GBP']
        
        for line in lines:
            for currency in target_currencies:
                if currency in line:
                    # Format: země|měna|množství|kód|kurz
                    # Example: Velká Británie|libra|1|GBP|28,146
                    parts = line.split('|')
                    if len(parts) >= 5 and parts[3].strip() == currency:
                        # Czech format uses comma as decimal separator
                        rate_str = parts[4].strip().replace(',', '.')
                        quantity = int(parts[2].strip()) if parts[2].strip().isdigit() else 1
                        # Rate is for the quantity, so divide by quantity to get rate per 1 unit
                        rate_per_unit = float(rate_str) / quantity
                        rates[currency] = rate_per_unit
                        break
        
        print(f"Fetched CNB rates: {rates}")
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
            if not cnb_rates:
                print("Could not fetch any rates from CNB, keeping existing rates")
                return
            
            now = datetime.utcnow()
            updated_currencies = []
            
            # Process each currency
            for currency, czk_rate in cnb_rates.items():
                if currency == 'CZK':  # Skip CZK itself
                    continue
                    
                czk_to_currency_rate = 1.0 / czk_rate
                
                # Update CURRENCY -> CZK rate (official CNB rate, no description)
                curr_czk = PredefinedRate.query.filter_by(
                    from_currency=currency, 
                    to_currency='CZK', 
                    description=None
                ).first()
                if curr_czk:
                    curr_czk.rate = czk_rate
                    curr_czk.updated = now
                else:
                    curr_czk = PredefinedRate(
                        from_currency=currency, 
                        to_currency='CZK', 
                        rate=czk_rate,
                        description=None
                    )
                    db.session.add(curr_czk)
                
                # Update CZK -> CURRENCY rate (official CNB rate, no description)
                czk_curr = PredefinedRate.query.filter_by(
                    from_currency='CZK', 
                    to_currency=currency, 
                    description=None
                ).first()
                if czk_curr:
                    czk_curr.rate = czk_to_currency_rate
                    czk_curr.updated = now
                else:
                    czk_curr = PredefinedRate(
                        from_currency='CZK', 
                        to_currency=currency, 
                        rate=czk_to_currency_rate,
                        description=None
                    )
                    db.session.add(czk_curr)
                
                # Add trivial rate (CURRENCY -> CURRENCY = 1.0, no description)
                curr_curr = PredefinedRate.query.filter_by(
                    from_currency=currency, 
                    to_currency=currency, 
                    description=None
                ).first()
                if not curr_curr:
                    curr_curr = PredefinedRate(
                        from_currency=currency, 
                        to_currency=currency, 
                        rate=1.0,
                        description=None
                    )
                    db.session.add(curr_curr)
                
                updated_currencies.append(currency)
                print(f"Updated rates: {currency}->CZK = {czk_rate:.4f}, CZK->{currency} = {czk_to_currency_rate:.6f}")
            
            # Add CZK -> CZK trivial rate if not exists (no description)
            czk_czk = PredefinedRate.query.filter_by(
                from_currency='CZK', 
                to_currency='CZK', 
                description=None
            ).first()
            if not czk_czk:
                czk_czk = PredefinedRate(
                    from_currency='CZK', 
                    to_currency='CZK', 
                    rate=1.0,
                    description=None
                )
                db.session.add(czk_czk)
            
            db.session.commit()
            print(f"Successfully updated rates for currencies: {', '.join(updated_currencies)}")
    
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

    # Seed default predefined rates if missing (BASE currency GBP)
    @app.before_first_request
    def seed_rates():
        with app.app_context():
            # Add trivial rates (currency to itself)
            trivial_currencies = ['GBP', 'CZK']
            for currency in trivial_currencies:
                if not PredefinedRate.query.filter_by(from_currency=currency, to_currency=currency).first():
                    db.session.add(PredefinedRate(from_currency=currency, to_currency=currency, rate=1.0))
            
            db.session.commit()

    @bp.route('/rates', methods=['GET'])
    def list_rates():
        rates = PredefinedRate.query.order_by(PredefinedRate.from_currency, PredefinedRate.to_currency, PredefinedRate.description).all()
        return {"rates": [
            {
                "id": r.id, 
                "from_currency": r.from_currency, 
                "to_currency": r.to_currency, 
                "rate": r.rate,
                "description": r.description,
                "name": f"{r.from_currency}-{r.to_currency}" + (f" ({r.description})" if r.description else "")
            } for r in rates
        ]}
    
    @bp.route('/rates', methods=['POST'])
    def add_rate():
        data = request.get_json() or {}
        required = ['from_currency', 'to_currency', 'rate']
        if not all(k in data for k in required):
            return {"error": "missing required fields: from_currency, to_currency, rate"}, 400
        
        from_curr = data['from_currency'].upper()
        to_curr = data['to_currency'].upper()
        rate_value = float(data['rate'])
        description = data.get('description', '').strip() or None
        
        if rate_value <= 0:
            return {"error": "rate must be positive"}, 400
        
        # Check if rate with same description already exists
        existing = PredefinedRate.query.filter_by(
            from_currency=from_curr, 
            to_currency=to_curr, 
            description=description
        ).first()
        if existing:
            desc_part = f" ({description})" if description else ""
            return {"error": f"rate {from_curr}-{to_curr}{desc_part} already exists"}, 409
        
        # Add the primary rate
        rate = PredefinedRate(
            from_currency=from_curr, 
            to_currency=to_curr, 
            rate=rate_value,
            description=description
        )
        db.session.add(rate)
        
        # For user-added rates with description, don't auto-add inverse
        # Only auto-add inverse for system rates (CNB updates) without description
        inverse_added = False
        if not description and from_curr != to_curr:
            inverse_existing = PredefinedRate.query.filter_by(
                from_currency=to_curr, 
                to_currency=from_curr,
                description=None
            ).first()
            if not inverse_existing:
                inverse_rate = PredefinedRate(
                    from_currency=to_curr, 
                    to_currency=from_curr, 
                    rate=1.0/rate_value,
                    description=None
                )
                db.session.add(inverse_rate)
                inverse_added = True
        
        db.session.commit()
        return {"id": rate.id, "inverse_added": inverse_added}
    
    @bp.route('/rates/<int:rate_id>', methods=['PUT'])
    def update_rate(rate_id):
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
            
        data = request.get_json() or {}
        if 'rate' not in data:
            return {"error": "missing rate field"}, 400
            
        new_rate_value = float(data['rate'])
        if new_rate_value <= 0:
            return {"error": "rate must be positive"}, 400
            
        old_rate_value = rate.rate
        rate.rate = new_rate_value
        rate.updated = datetime.utcnow()
        
        # Update inverse rate if it exists and currencies are different
        inverse_updated = False
        if rate.from_currency != rate.to_currency:
            inverse = PredefinedRate.query.filter_by(
                from_currency=rate.to_currency, 
                to_currency=rate.from_currency
            ).first()
            if inverse:
                inverse.rate = 1.0 / new_rate_value
                inverse.updated = datetime.utcnow()
                inverse_updated = True
        
        db.session.commit()
        return {"id": rate.id, "rate": rate.rate, "inverse_updated": inverse_updated}
    
    @bp.route('/rates/<int:rate_id>', methods=['DELETE'])
    def delete_rate(rate_id):
        rate = db.session.get(PredefinedRate, rate_id)
        if rate is None:
            return {"error": "rate not found"}, 404
        
        # Check if rate is being used
        income_count = Income.query.filter_by(rate_id=rate_id).count()
        cost_count = Cost.query.filter_by(rate_id=rate_id).count()
        expected_count = ExpectedCost.query.filter_by(rate_id=rate_id).count()
        
        if income_count > 0 or cost_count > 0 or expected_count > 0:
            return {"error": f"rate is being used by {income_count} incomes, {cost_count} costs, {expected_count} expected costs"}, 409
        
        from_curr = rate.from_currency
        to_curr = rate.to_currency
        
        db.session.delete(rate)
        
        # Also delete inverse rate if it exists and is not being used
        if from_curr != to_curr:
            inverse = PredefinedRate.query.filter_by(from_currency=to_curr, to_currency=from_curr).first()
            if inverse:
                inverse_income_count = Income.query.filter_by(rate_id=inverse.id).count()
                inverse_cost_count = Cost.query.filter_by(rate_id=inverse.id).count()
                inverse_expected_count = ExpectedCost.query.filter_by(rate_id=inverse.id).count()
                
                if inverse_income_count == 0 and inverse_cost_count == 0 and inverse_expected_count == 0:
                    db.session.delete(inverse)
        
        db.session.commit()
        return {"deleted": rate_id}

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
        total_income = db.session.query(db.func.coalesce(db.func.sum(Income.norm_amount), 0.0)).scalar()
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
        db_rate = PredefinedRate.query.filter_by(from_currency='GBP', to_currency='CZK').first()
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

    @bp.route('/income', methods=['POST'])
    def add_income():
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
        
        income = Income(description=desc, 
                        amount=amount,
                        currency=currency, 
                        rate_id=rate_id,
                        norm_rate=rate_value,  # Store historical rate
                        norm_amount=norm_amount)
        db.session.add(income)
        db.session.commit()
        return {"id": income.id, "norm_amount": norm_amount, "currency": currency}

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
                "rate_name": f"{i.rate.from_currency}-{i.rate.to_currency}" if i.rate else "legacy"
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
                    expected_ref_id=expected_ref_id)
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
        
        db.session.commit()
        return {"id": cost.id, "amount": cost.amount, "norm_amount": cost.norm_amount, "description": cost.description, "currency": cost.currency, "rate_id": cost.rate_id}

    @bp.route('/expected', methods=['GET', 'POST'])
    def expected_list_or_create():
        if request.method == 'GET':
            expected = ExpectedCost.query.all()
            return {"expected": [
                {
                    "id": e.id, 
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
    real_app.run(host='0.0.0.0', port=5000, debug=False)
