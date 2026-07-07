import sys
import os
import datetime
import json
import re
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from google.adk.workflow import Workflow, START, node, Edge
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App, ResumabilityConfig
from google.genai import types

from app.config import config

# --- 1. Schemas ---

class MealPlanningRequest(BaseModel):
    query: str = Field(description="The user's request for meal planning (e.g. duration, diet, restrictions).")

class Recipe(BaseModel):
    name: str = Field(description="Name of the dish")
    ingredients: List[str] = Field(default_factory=list, description="List of ingredients with measurements")
    instructions: List[str] = Field(default_factory=list, description="Step-by-step preparation steps")

class DailyPlan(BaseModel):
    day: str = Field(description="Day name, e.g. Day 1, Monday")
    breakfast: Optional[Recipe] = Field(None, description="Breakfast recipe")
    lunch: Optional[Recipe] = Field(None, description="Lunch recipe")
    dinner: Optional[Recipe] = Field(None, description="Dinner recipe")

class MealPlan(BaseModel):
    meals: List[DailyPlan] = Field(description="List of daily plans")
    dietary_notes: Optional[str] = Field(None, description="Nutritional or dietary advice")

class GroceryList(BaseModel):
    categories: Dict[str, List[str]] = Field(description="Categorized grocery list, e.g. Produce, Meat, Pantry")
    substitutions: List[str] = Field(description="Alternative ingredients suggested")

class MealPlanningResponse(BaseModel):
    meal_plan: MealPlan = Field(description="The finalized meal plan")
    grocery_list: GroceryList = Field(description="The compiled grocery list")
    audit_notes: Optional[str] = Field(None, description="Security and audit notes")

# --- 2. MCP Toolset ---

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
            env={**os.environ, "NUTRICHEF_MCP": "1"}
        ),
    ),
)

# --- 3. Sub-Agents ---

meal_planner = LlmAgent(
    name="meal_planner",
    model=config.model,
    instruction="""You are an expert chef and certified nutritionist with extensive culinary knowledge spanning all global cuisines (e.g. Italian, Indian, Japanese, Ethiopian, Mexican, etc.).
Your goal is to design healthy, nutritionally balanced recipe plans.
Adhere strictly to any dietary restrictions, allergies, and duration requested.
Provide recipes with exact quantities and clear instructions.
You can use the search_recipe_database tool to search for specific recipes or ideas.
If the database does not contain a recipe or cuisine, use your own expert culinary knowledge to design the recipe from scratch.
You can use the check_fridge_inventory tool to see what is available in the fridge and prioritize using those ingredients.

CRITICAL DURATION RULE:
- You MUST generate exactly the number of daily plans matching the requested duration (e.g. if the user asks for a 7-day meal plan, your 'meals' list MUST contain exactly 7 daily plan objects, one for each day. Do NOT combine multiple days into a single daily plan object).

CRITICAL MACRONUTRIENT RULE:
- If the user requests specific macronutrient levels (e.g. protein, calories, carbs, fat), you MUST include these details directly in the recipe 'name' (e.g., "Grilled Salmon (35g Protein)") or in the 'instructions' list, as the output schema does not contain separate fields for macronutrients.

CRITICAL CONCISENESS RULE:
- Keep recipe descriptions, ingredient lists, and instructions concise. Use 2-4 short step sentences for instructions, and keep ingredients brief. This is especially critical for multi-day plans (like 7 days) to ensure all daily plans fit within the token limit without getting truncated.

CRITICAL JSON RULES:
- You MUST respond ONLY with a valid JSON object matching the MealPlan schema.
- NEVER wrap your response in markdown code fences or markdown blocks (e.g. do NOT use ```json ... ```).
- ABSOLUTELY NEVER return raw text, conversational sentences, or ask questions directly. Every single response you emit must be a parseable JSON object matching the schema.
- If the user's input/request does not contain enough information to generate a meal plan, or if you are asked to modify a plan but lack context, do NOT ask questions or return raw text. Instead, generate a standard 1-day balanced meal plan (using common ingredients) and specify any questions, requirements, or clarifications for further customization in the 'dietary_notes' field.""",
    tools=[mcp_toolset],
    output_schema=MealPlan,
    output_key="meal_plan",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=8192,
    ),
    description="Generates custom meal plans and recipes matching user dietary needs.",
)

grocery_coordinator = LlmAgent(
    name="grocery_coordinator",
    model=config.model,
    instruction="""You are a smart grocery shopping assistant with global knowledge of ingredients, produce, and markets.
Your goal is to take a detailed meal plan and list of recipes, consolidate the ingredients,
and compile them into a beautifully categorized shopping list.
Also provide helpful ingredient substitutions based on the user's diet (e.g. gluten-free alternatives).
You can check ingredient prices using the get_ingredient_prices tool to estimate the budget.
If a price is not found, estimate a reasonable market price and proceed without raising an error.
You can use the check_fridge_inventory tool to filter out ingredients the user already has in their fridge.

CRITICAL CONCISENESS RULE:
- Keep the category list and substitutions list concise and direct.

CRITICAL JSON RULES:
- You MUST respond ONLY with a valid JSON object matching the GroceryList schema.
- NEVER wrap your response in markdown code fences or markdown blocks (e.g. do NOT use ```json ... ```).
- If the input meal plan is empty or invalid, do NOT return conversational text or errors. Instead, return a valid JSON object matching the schema with empty categories and empty substitutions.""",
    tools=[mcp_toolset],
    output_schema=GroceryList,
    output_key="grocery_list",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=8192,
    ),
    description="Compiles consolidated, categorized grocery lists and suggests substitutions from a meal plan.",
)

# --- 4. Orchestrator ---

def get_orchestrator_instruction(ctx: ReadonlyContext) -> str:
    meal_plan = ctx.state.get("meal_plan")
    user_feedback = ctx.state.get("user_feedback")
    
    plan_context = ""
    if meal_plan:
        plan_context += f"\n\nCURRENT PROPOSED MEAL PLAN (for your reference):\n{json.dumps(meal_plan, indent=2)}"
    if user_feedback:
        plan_context += f"\n\nUSER DISAPPROVAL FEEDBACK:\n{user_feedback}"
        
    return f"""You are the NutriChef Concierge Orchestrator.
Your goal is to plan meals and compile grocery lists. You support requests for any global cuisine, diet, or food culture from anywhere in the world.{plan_context}

You must coordinate the process by delegating tasks or answering the user directly:
1. If the user is asking a general question, requesting explanations, or explaining a concern (e.g. "Why did you include eggs?", "What is keto?", "How many calories is this?", or "Explain the nutrition"), do NOT call any tools. Answer their question directly, accurately, and concisely in a friendly conversational style.
2. If the user is asking to modify or customize the meal plan (e.g. "swap avocado for spinach", "make it 3 days", "no eggs", or giving specific feedback on changes), you MUST call the meal_planner tool to regenerate/modify the plan. When calling the meal_planner tool, you MUST explicitly provide the original request, the current/previous meal plan, and the user's feedback. After the tool completes, output a friendly confirmation message stating that the plan has been modified and is ready for review. Do NOT output raw JSON in your response.
3. If the user approves the plan (e.g. "Yes", "looks good", "approve") or requests grocery list preparation, call the grocery_coordinator tool.

Always be conversational, helpful, and concise. Do NOT output raw JSON in your final responses to the user."""

orchestrator = LlmAgent(
    name="orchestrator",
    model=config.model,
    instruction=get_orchestrator_instruction,
    tools=[AgentTool(meal_planner), AgentTool(grocery_coordinator)],
    description="Orchestrates the meal planning process and coordinates sub-agents.",
)

# --- 5. Workflow Function Nodes ---

@node
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    # Extract query text from types.Content, dict, or raw object
    if hasattr(node_input, "parts") and node_input.parts:
        user_query = "".join(part.text for part in node_input.parts if hasattr(part, "text") and part.text)
    elif isinstance(node_input, dict):
        user_query = node_input.get("query", "")
    elif hasattr(node_input, "query"):
        user_query = node_input.query
    else:
        user_query = str(node_input)
    
    # PII Scrubbing
    email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    phone_pattern = r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'
    
    scrubbed_query = user_query
    if config.pii_redaction_enabled:
        scrubbed_query = re.sub(email_pattern, "[REDACTED_EMAIL]", scrubbed_query)
        scrubbed_query = re.sub(phone_pattern, "[REDACTED_PHONE]", scrubbed_query)
    
    # Prompt Injection Detection
    injection_keywords = ["system prompt", "ignore previous instructions", "override settings", "disable safety", "reveal prompt"]
    has_injection = any(kw in scrubbed_query.lower() for kw in injection_keywords)
    
    # Domain-specific rule: block toxic/non-food items
    toxic_keywords = ["cyanide", "arsenic", "bleach", "detergent", "poison", "tide pod"]
    has_toxic = any(tk in scrubbed_query.lower() for tk in toxic_keywords)
    
    audit_log = {
        "timestamp": datetime.datetime.now().isoformat(),
        "session_id": ctx.session.id,
        "pii_detected": scrubbed_query != user_query,
        "injection_detected": has_injection,
        "toxic_detected": has_toxic,
        "severity": "CRITICAL" if (has_injection or has_toxic) else ("WARNING" if scrubbed_query != user_query else "INFO")
    }
    
    # Structured JSON log
    print(f"[SECURITY AUDIT] {json.dumps(audit_log)}")
    
    if has_injection or has_toxic:
        return Event(
            output={"error": "Potential prompt injection or toxic input detected."},
            route="violation",
            state={"audit_log": json.dumps(audit_log), "security_status": "FAILED"}
        )
    
    return Event(
        output=MealPlanningRequest(query=scrubbed_query),
        route="clear",
        state={"scrubbed_query": scrubbed_query, "audit_log": json.dumps(audit_log), "security_status": "PASSED"}
    )

@node
def security_violation_node(node_input: Any) -> Event:
    return Event(
        output={"error": "Access Denied: Blocked by security policy."},
        state={"security_status": "FAILED"}
    )

@node
async def meal_review(ctx: Context, node_input: Any) -> Event:
    meal_plan_dict = ctx.state.get("meal_plan")
    if not meal_plan_dict:
        yield Event(output=None, route="disapproved")
        return
        
    meal_plan = MealPlan(**meal_plan_dict)
    
    # If the user hasn't responded yet, pause and request input
    if not ctx.resume_inputs:
        # Construct permanent review message
        plan_msg = "### 📅 Proposed Meal Plan for Your Review:\n\n"
        for meal in meal_plan.meals:
            plan_msg += f"**{meal.day}**:\n"
            if meal.breakfast:
                plan_msg += f"  * 🍳 *Breakfast*: {meal.breakfast.name}\n"
            if meal.lunch:
                plan_msg += f"  * 🥗 *Lunch*: {meal.lunch.name}\n"
            if meal.dinner:
                plan_msg += f"  * 🥩 *Dinner*: {meal.dinner.name}\n"
            plan_msg += "\n"
            
        yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=plan_msg)]))
        
        yield RequestInput(
            interrupt_id="approve_meal_plan",
            message="Do you approve this plan? (Yes / No)"
        )
        return
    
    # Read response
    user_response = ctx.resume_inputs.get("approve_meal_plan", "").strip()
    if "yes" in user_response.lower():
        yield Event(output=meal_plan, route="approved")
        return
        
    # Check if they gave a simple 'no' without explaining why
    simple_no_patterns = [r"^no$", r"^n$", r"^nope$", r"^don't approve$", r"^disapprove$"]
    is_simple_no = any(re.match(pattern, user_response.lower()) for pattern in simple_no_patterns) or len(user_response) <= 3
    
    if is_simple_no:
        if "specific_feedback" not in ctx.resume_inputs:
            yield RequestInput(
                interrupt_id="specific_feedback",
                message="Got it! What specific changes would you like to make? (e.g. 'swap salmon for chicken', 'make it vegetarian', or 'suggest different breakfast options')"
            )
            return
            
    # Read the specific feedback if they responded to the second prompt, otherwise fallback
    final_feedback = ctx.resume_inputs.get("specific_feedback", user_response)
    yield Event(output=final_feedback, route="disapproved", state={"user_feedback": final_feedback})

@node
async def grocery_coordinator_node(ctx: Context, node_input: Any) -> Event:
    # Call the grocery_coordinator LlmAgent directly
    async for event in grocery_coordinator.run_async(ctx):
        yield event

@node
def final_response(ctx: Context, node_input: Any):
    if ctx.state.get("security_status") == "FAILED":
        msg = "❌ Access Denied: Your input contains blocked patterns or potential injection attempts."
        yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=msg)]))
        yield Event(output=MealPlanningResponse(
            meal_plan=MealPlan(meals=[], dietary_notes="Blocked by security checkpoint."),
            grocery_list=GroceryList(categories={}, substitutions=[]),
            audit_notes="Blocked due to security policy."
        ))
        return
        
    meal_plan_dict = ctx.state.get("meal_plan")
    grocery_list_dict = ctx.state.get("grocery_list")
    audit_log = ctx.state.get("audit_log")
    
    meal_plan = MealPlan(**meal_plan_dict) if meal_plan_dict else MealPlan(meals=[], dietary_notes="")
    grocery_list = GroceryList(**grocery_list_dict) if grocery_list_dict else GroceryList(categories={}, substitutions=[])
    
    # Construct a beautiful, simplified markdown output for UI
    response_text = "### 🍳 Your Personalized NutriChef Plan\n\n"
    
    response_text += "**📅 Meal Plan**\n"
    for meal in meal_plan.meals:
        response_text += f"* **{meal.day}**:\n"
        if meal.breakfast:
            response_text += f"  * 🍳 *Breakfast*: {meal.breakfast.name}\n"
        if meal.lunch:
            response_text += f"  * 🥗 *Lunch*: {meal.lunch.name}\n"
        if meal.dinner:
            response_text += f"  * 🥩 *Dinner*: {meal.dinner.name}\n"
    response_text += "\n"
    
    response_text += "**🛒 Categorized Grocery List**\n"
    for category, items in grocery_list.categories.items():
        item_list = ", ".join(items)
        response_text += f"* **{category}**: {item_list}\n"
    response_text += "\n"
        
    if grocery_list.substitutions:
        sub_list = ", ".join(grocery_list.substitutions)
        response_text += f"**🔄 Smart Substitutions**\n* {sub_list}\n\n"
        
    if meal_plan.dietary_notes:
        response_text += f"💡 *Note*: {meal_plan.dietary_notes}\n\n"
        
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=response_text)]))
    yield Event(output=MealPlanningResponse(
        meal_plan=meal_plan,
        grocery_list=grocery_list,
        audit_notes=audit_log
    ))

# --- 6. Workflow Definitions ---

root_agent = Workflow(
    name="nutrichef",
    description="NutriChef multi-agent meal planner and shopping list concierge",
    input_schema=None,
    output_schema=MealPlanningResponse,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=orchestrator, route="clear"),
        Edge(from_node=security_checkpoint, to_node=security_violation_node, route="violation"),
        Edge(from_node=orchestrator, to_node=meal_review),
        Edge(from_node=meal_review, to_node=orchestrator, route="disapproved"),
        Edge(from_node=meal_review, to_node=grocery_coordinator_node, route="approved"),
        Edge(from_node=grocery_coordinator_node, to_node=final_response),
        Edge(from_node=security_violation_node, to_node=final_response)
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
