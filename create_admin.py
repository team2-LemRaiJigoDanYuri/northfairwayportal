"""Create the first NFH-HOA Administrator account safely.

Run after schema.sql on a fresh database:
    python create_admin.py

The password is requested interactively and is never stored in this script.
"""
import getpass
import os
import re
import sys
from dotenv import load_dotenv
import pymysql
import pymysql.cursors
from werkzeug.security import generate_password_hash

load_dotenv()


def ask(label, required=True):
    while True:
        value=input(f"{label}: ").strip()
        if value or not required:
            return value
        print(f"{label} is required.")


def main():
    print("NFH-HOA Initial Administrator Setup")
    first=ask("First name")
    last=ask("Last name")
    email=ask("Email").lower()
    username=ask("Username")
    mobile=ask("Mobile number (09XXXXXXXXX)")
    if not re.match(r'^09\d{9}$',mobile):
        sys.exit("Invalid mobile number. Use 11 digits beginning with 09.")
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',email):
        sys.exit("Invalid email address.")
    password=getpass.getpass("Password (minimum 8 characters): ")
    confirm=getpass.getpass("Confirm password: ")
    if len(password)<8 or password!=confirm:
        sys.exit("Passwords must match and contain at least 8 characters.")

    conn=pymysql.connect(
        host=os.getenv('MYSQL_HOST','localhost'),
        user=os.getenv('MYSQL_USER','root'),
        password=os.getenv('MYSQL_PASSWORD',''),
        database=os.getenv('MYSQL_DB','nfhsystem'),
        port=int(os.getenv('MYSQL_PORT','3306')),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(%s) OR LOWER(email)=LOWER(%s)",(username,email))
            if cur.fetchone():
                sys.exit("That username or email already exists.")
            cur.execute("""INSERT INTO users (first_name,last_name,email,username,mobile,password_hash,role,status,verification_status)
                           VALUES (%s,%s,%s,%s,%s,%s,'Admin','Active','Verified')""",
                        (first,last,email,username,mobile,generate_password_hash(password,method='pbkdf2:sha256')))
        print("Administrator account created successfully.")
    finally:
        conn.close()


if __name__=='__main__':
    main()
