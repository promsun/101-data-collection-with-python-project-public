"""
=================================================================
                 Data Collection with Python
=================================================================
"""

import os
from datetime import datetime

import pandas as pd
import requests
import sqlalchemy
from dotenv import load_dotenv

# =================================================================
#                    ENVIRONMENT SETUP
# =================================================================
# Determine environment (default: dev)
ENV = os.getenv("ENV", "dev")
ENV_FILES = {"dev": ".env.dev", "prod": ".env.prod"}

# Load environment variables based on ENV
env_file = ENV_FILES.get(ENV, ".env.dev")

if os.path.exists(env_file):
    load_dotenv(env_file)
    print(f"\n[INFO] Loaded environment variables from {env_file}")
else:
    print(
        f"\n[WARNING] '{env_file}' file not found. Please create it with required credentials."
    )
    print("[WARNING] Proceeding with system environment variables...")

# =================================================================
#                   DATA PATHS CONFIGURATION
# =================================================================
# Date for data versioning (format: YYYY-MM-DD)
RUN_DATE = datetime.now().strftime("%Y-%m-%d")

# Data directories (partitioned by date)
DATA_DIRS = {
    "raw": os.path.join("data", "raw", RUN_DATE),
    "processed": os.path.join("data", "processed", RUN_DATE),
}

# Output files
RAW_FILES = {
    "products": os.path.join(DATA_DIRS["raw"], "products.parquet"),
    "customers": os.path.join(DATA_DIRS["raw"], "customers.parquet"),
    "orders": os.path.join(DATA_DIRS["raw"], "orders.parquet"),
    "order_items": os.path.join(DATA_DIRS["raw"], "order_items.parquet"),
    "conversion_rate": os.path.join(DATA_DIRS["raw"], "conversion_rate.parquet"),
}

PROCESSED_FILES = {
    "final_output": os.path.join(DATA_DIRS["processed"], "final_output.parquet"),
    "final_output_csv": os.path.join(DATA_DIRS["processed"], "final_output.csv"),
}

# =================================================================
#                     DATABASE CONFIGURATION
# =================================================================
DATABASE_URL = os.getenv("DATABASE_URL")

DB_TABLES = {
    "products": "public.products",
    "customers": "public.customers",
    "orders": "public.orders",
    "order_items": "public.order_items",
}

# =================================================================
#                      API CONFIGURATION
# =================================================================
CURRENCY_API_URL = (
    "https://api.frankfurter.dev/v1/2016-01-01..2018-12-31?base=BRL&symbols=THB"
)

# =================================================================
#                      OTHER CONFIGURATION
# =================================================================
# Pandas display options
pd.set_option("display.max_columns", None)


# =================================================================
#         STEP 1: Extract data from PostgreSQL database
# =================================================================


def extract_postgres_data():
    """
    Extract data from PostgreSQL database and save as raw parquet files

    This function:
    1. Connects to PostgreSQL database
    2. Reads all tables (products, customers, orders, order_items)
    3. Saves raw data as parquet files for data lake pattern
    4. Returns dataframes for immediate processing (optional)
    """
    print("\n" + "=" * 70)
    print("  📝 STEP 1: Extracting data from PostgreSQL")
    print("=" * 70)

    # fmt: off
    # Define table queries for flexibility (Format: table_name, SQL query, output path)
    table_queries = [
        ("products", f"SELECT * FROM {DB_TABLES['products']};", RAW_FILES["products"]),
        ("customers", f"SELECT * FROM {DB_TABLES['customers']};", RAW_FILES["customers"]),
        ("orders", f"SELECT * FROM {DB_TABLES['orders']};", RAW_FILES["orders"]),
        ("order_items", f"SELECT * FROM {DB_TABLES['order_items']};", RAW_FILES["order_items"]),
    ]
    # fmt: on

    # Validate DATABASE_URL exists
    if not DATABASE_URL:
        raise EnvironmentError(f"DATABASE_URL not found. Please configure {env_file}")

    print("[INFO] Connecting to database...")

    # Create database engine and ensure proper cleanup
    engine = sqlalchemy.create_engine(DATABASE_URL)

    try:
        with engine.connect() as connection:
            print("[SUCCESS] Database connection established")

            tables_data = {}

            for table_key, query, output_path in table_queries:
                print(f"[INFO] Reading table: {table_key}")
                df = pd.read_sql(query, connection)

                # Save raw data as parquet
                df.to_parquet(output_path, index=False)

                tables_data[table_key] = df
                print(f"[INFO]   -> Saved {len(df)} rows to {output_path}")

        print("\n[SUCCESS] All database tables extracted successfully")
        return tables_data

    finally:
        engine.dispose()
        print("\n[INFO] Database connection closed")


# =================================================================
#         STEP 2: Fetch conversion rate data from API
# =================================================================


def extract_api_data():
    """
    Fetch currency conversion rate from Frankfurter API

    This function:
    1. Calls Frankfurter API for BRL to THB conversion rates (2016-2018)
    2. Transforms JSON response to DataFrame format
    3. Fills missing dates (weekends/holidays) using forward fill
    4. Saves raw data as parquet file
    """
    print("\n" + "=" * 70)
    print("  📝 STEP 2: Fetching conversion rate from API")
    print("=" * 70)

    print("[INFO] Fetching data from Frankfurter API...")
    print(f"[INFO]   URL: {CURRENCY_API_URL}")

    response = requests.get(CURRENCY_API_URL, timeout=30)
    response.raise_for_status()  # Raise exception for HTTP errors

    data = response.json()
    rates = data["rates"]
    print(f"[INFO] Received {len(rates)} conversion rate records")

    # Transform nested JSON to flat list of dictionaries
    conversion_data = [
        {
            "date": date,
            "brl_thb": rate_info["THB"],
        }
        for date, rate_info in rates.items()
    ]

    # Convert to DataFrame
    conversion_rate = pd.DataFrame(conversion_data)
    conversion_rate["date"] = pd.to_datetime(conversion_rate["date"])

    # Fill missing dates (weekends & holidays) using forward fill
    print("[INFO] Filling missing dates (weekends/holidays)...")
    all_dates = pd.date_range(
        start=conversion_rate["date"].min(),
        end=conversion_rate["date"].max(),
        freq="D",
    )
    all_dates_df = pd.DataFrame({"date": all_dates})
    conversion_rate = all_dates_df.merge(conversion_rate, how="left", on="date").ffill()

    print(f"[INFO] After filling missing dates: {len(conversion_rate)} total records")

    # Save raw data as parquet
    output_path = RAW_FILES["conversion_rate"]
    conversion_rate.to_parquet(output_path, index=False)
    print(f"[INFO]   -> Saved to: {output_path}")
    print("[SUCCESS] API data extracted successfully")

    return conversion_rate


# =================================================================
#              STEP 3: Transform and merge datasets
# =================================================================


def transform_data():
    """
    Load raw data from parquet files and perform transformations

    This function:
    1. Reads raw parquet files (not from database/API directly)
    2. Merges order_items with products, orders, and customers
    3. Joins with conversion rates
    4. Calculates THB prices
    5. Returns final transformed dataframe

    Note: This reads from parquet files so it can be run independently
    without re-fetching from database/API (orchestration-ready)
    """
    print("\n" + "=" * 70)
    print("  📝 STEP 3: Transforming and merging data")
    print("=" * 70)

    # Load raw data from parquet files
    print("[INFO] Loading raw data from parquet files...")
    products = pd.read_parquet(RAW_FILES["products"])
    customers = pd.read_parquet(RAW_FILES["customers"])
    orders = pd.read_parquet(RAW_FILES["orders"])
    order_items = pd.read_parquet(RAW_FILES["order_items"])
    conversion_rate = pd.read_parquet(RAW_FILES["conversion_rate"])

    print(f"[1/5]   Products: {len(products)} rows")
    print(f"[2/5]   Customers: {len(customers)} rows")
    print(f"[3/5]   Orders: {len(orders)} rows")
    print(f"[4/5]   Order items: {len(order_items)} rows")
    print(f"[5/5]   Conversion rates: {len(conversion_rate)} rows")
    print("[SUCCESS] All raw data loaded successfully")

    # Merge order_items with products
    print("\n[INFO] Merging datasets...")

    # Merge orders with customers
    print("[1/8] Merging orders with customers...")
    orders_w_customers = orders.merge(
        customers, how="left", left_on="customer_id", right_on="customer_id"
    )
    # Merge order_items with products and orders_w_customers
    print("[2/8] Merging order_items with products and orders...")
    merged = order_items.merge(
        products, how="left", left_on="product_id", right_on="product_id"
    ).merge(orders_w_customers, how="left", left_on="order_id", right_on="order_id")

    # Drop unnecessary columns
    print("[3/8] Dropping unnecessary columns...")
    merged = merged.drop(
        ["product_name_lenght", "product_description_lenght", "product_photos_qty"],
        axis=1,
        errors="ignore",
    )

    # Prepare date column for conversion rate join
    print("[4/8] Preparing date column for conversion rate join...")
    merged["order_date"] = pd.to_datetime(
        merged["order_purchase_timestamp"]
    ).dt.normalize()

    # Merge with conversion rates
    print("[5/8] Merging with conversion rates...")
    merged = merged.merge(
        conversion_rate, how="left", left_on="order_date", right_on="date"
    )

    # Calculate THB prices
    print("[6/8] Calculating THB prices...")
    merged["thb_price"] = merged["price"] * merged["brl_thb"]
    merged["thb_freight_value"] = merged["freight_value"] * merged["brl_thb"]

    # Drop temporary columns
    print("[7/8] Dropping temporary columns...")
    merged = merged.drop(["date", "brl_thb"], axis=1, errors="ignore")

    # Rename columns with consistent naming convention
    print("[8/8] Renaming columns for consistency...")
    merged.columns = [
        "order_id",
        "order_item_id",
        "order_item_product_id",
        "order_item_seller_id",
        "order_item_shipping_limit_date",
        "order_item_price",
        "order_item_freight_value",
        "order_item_product_category_name",
        "order_item_product_weight_g",
        "order_item_product_length_cm",
        "order_item_product_height_cm",
        "order_item_product_width_cm",
        "order_customer_id",
        "order_status",
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
        "order_customer_unique_id",
        "order_customer_zip_code_prefix",
        "order_customer_city",
        "order_customer_state",
        "order_date",
        "order_item_thb_price",
        "order_item_thb_freight_value",
    ]

    print(
        f"[SUCCESS] Final merged dataset: {len(merged)} rows x {len(merged.columns)} columns"
    )
    print("\n[INFO] Sample of final data:")
    print(merged.head())

    return merged


# =================================================================
#                 STEP 4: Load processed data
# =================================================================


def load_processed_data(final_df):
    """
    Save processed data to output files

    This function:
    1. Saves final dataframe as parquet (for data lake)
    2. Saves final dataframe as CSV (for easy viewing/sharing)
    """
    print("\n" + "=" * 70)
    print("  📝 STEP 4: Saving processed data")
    print("=" * 70)

    # Save as parquet
    print("[INFO] Saving parquet file...")
    parquet_path = PROCESSED_FILES["final_output"]
    final_df.to_parquet(parquet_path, index=False)
    print(f"[INFO]   -> Saved to: {parquet_path}")

    # Save as CSV
    print("\n[INFO] Saving CSV file...")
    csv_path = PROCESSED_FILES["final_output_csv"]
    final_df.to_csv(csv_path, index=False)
    print(f"[INFO]   -> Saved to: {csv_path}")

    print("\n[SUCCESS] Processed data saved successfully")


# =================================================================
#                          MAIN PIPELINE
# =================================================================


def main():
    """
    Main data pipeline orchestration

    This function runs the complete ETL pipeline:
    1. Extract: Fetch data from PostgreSQL and API
    2. Transform: Merge and calculate final metrics
    3. Load: Save processed data to output files

    All steps are idempotent and can be run multiple times safely.
    Raw data is saved as parquet for data lake pattern.
    Processed data can be regenerated from raw data without re-fetching.
    """
    print("\n" + "=" * 70)
    print("  🚀 DATA COLLECTION PIPELINE - START")
    print(f"  📅 Run Date: {RUN_DATE}")
    print(f"  🌐 Environment: {ENV}")
    print("=" * 70)

    # Create directory structure if not exists
    print("[INFO] Creating data directories...")
    for dir_path in DATA_DIRS.values():
        os.makedirs(dir_path, exist_ok=True)
    print(f"[INFO]   Raw: {DATA_DIRS['raw']}")
    print(f"[INFO]   Processed: {DATA_DIRS['processed']}")

    try:
        # Step 1: Extract from PostgreSQL
        # extract_postgres_data()

        # Step 2: Extract from API
        extract_api_data()

        # Step 3: Transform data
        final_df = transform_data()

        # Step 4: Load processed data
        load_processed_data(final_df)

        print("\n" + "=" * 70)
        print("  ✅ PIPELINE COMPLETED SUCCESSFULLY")
        print("=" * 70)
        print(f"[SUCCESS] Raw data saved to: {DATA_DIRS['raw']}")
        print(f"[SUCCESS] Processed data saved to: {DATA_DIRS['processed']}")

    except Exception as e:
        print("\n" + "=" * 70)
        print("  ❌ PIPELINE FAILED")
        print("=" * 70)
        print(f"[ERROR] {e}")
        raise


if __name__ == "__main__":
    main()
