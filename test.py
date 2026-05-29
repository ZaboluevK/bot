from dotenv import load_dotenv
import os

load_dotenv() 
import os
items = os.getenv("ADMIN_IDS").split(",")
print(items)

