import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nutrichef")

# Recipes Mock DB
RECIPES_DB = {
    "italian": {
        "tomato": "Caprese Salad (Tomatoes, Mozzarella, Basil, Olive Oil, Balsamic Vinegar) - Prep: 10 mins",
        "pasta": "Spaghetti Pomodoro (Spaghetti, Tomatoes, Garlic, Basil, Olive Oil) - Prep: 20 mins"
    },
    "mexican": {
        "chicken": "Chicken Fajitas (Chicken Breast, Bell Peppers, Onions, Fajita Seasoning, Tortillas) - Prep: 25 mins",
        "avocado": "Guacamole & Chips (Avocados, Lime, Cilantro, Onion, Tomato, Tortilla Chips) - Prep: 10 mins"
    },
    "keto": {
        "eggs": "Keto Avocado Egg Salad (Hard-boiled Eggs, Avocado, Mayonnaise, Dijon Mustard, Chives) - Prep: 15 mins",
        "salmon": "Pan-seared Salmon with Broccoli (Salmon Fillet, Broccoli, Butter, Lemon, Garlic) - Prep: 20 mins"
    }
}

PRICES_DB = {
    "spaghetti": "$1.50 per pack",
    "tomatoes": "$2.00 per lb",
    "chicken breast": "$4.50 per lb",
    "avocados": "$1.25 each",
    "eggs": "$3.00 per dozen",
    "salmon fillet": "$9.00 per lb",
    "broccoli": "$1.80 per head",
    "mozzarella": "$3.50 per pack",
    "lettuce": "$1.50 per head",
    "spinach": "$2.00 per bag"
}

FRIDGE_INVENTORY = [
    "eggs", "butter", "olive oil", "garlic", "salt", "pepper", "lemon"
]

@mcp.tool()
def search_recipe_database(cuisine: str, main_ingredient: str) -> str:
    """Search the mock recipe database.
    
    Args:
        cuisine: Cuisine type (e.g. italian, mexican, keto).
        main_ingredient: A key ingredient to search for.
    """
    cuisine = cuisine.lower().strip()
    ingredient = main_ingredient.lower().strip()
    
    recipes = RECIPES_DB.get(cuisine, {})
    for ing, recipe in recipes.items():
        if ing in ingredient or ingredient in ing:
            return f"Found Recipe ({cuisine} - {main_ingredient}): {recipe}"
            
    return f"No recipes found for cuisine '{cuisine}' featuring '{main_ingredient}'."

@mcp.tool()
def get_ingredient_prices(ingredients: list[str]) -> str:
    """Retrieve market prices for specified ingredients.
    
    Args:
        ingredients: List of ingredient names.
    """
    prices = {}
    for ing in ingredients:
        ing_clean = ing.lower().strip()
        matched = False
        for k, v in PRICES_DB.items():
            if k in ing_clean or ing_clean in k:
                prices[k] = v
                matched = True
                break
        if not matched:
            prices[ing] = "Price not found (estimated $2.50)"
            
    return "Ingredient Price Report:\n" + "\n".join(f"- {k}: {v}" for k, v in prices.items())

@mcp.tool()
def check_fridge_inventory() -> str:
    """Check ingredients currently in the user's smart fridge to prevent buying duplicates."""
    return "Smart Fridge Inventory:\n" + "\n".join(f"- {item}" for item in FRIDGE_INVENTORY)

if __name__ == "__main__":
    mcp.run()
