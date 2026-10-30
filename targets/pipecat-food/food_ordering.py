#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""A food ordering flow example for Pipecat Flows.

This example demonstrates a food ordering system using flows where
conversation paths are determined at runtime. The flow handles:

1. Initial greeting and food type selection (pizza or sushi)
2. Order details collection based on food type
3. Order confirmation and revision
4. Order completion

VOCTO: this file is Pipecat's upstream example (pipecat-ai-flows 1.4.0, examples/food_ordering.py,
BSD 2-Clause, see LICENSE-pipecat-flows) with only these changes, each marked "VOCTO:":
  * the transport, speech and LLM setup (run_bot / bot / main) moved to bot.py, which runs local
    models and answers calls in the Twilio Media Streams format;
  * Flows is imported from pipecat.flows (Pipecat 1.15 ships it; the standalone package is retired);
  * complete_order sends the order to the restaurant's order API (the upstream example never sends
    it anywhere), and get_delivery_estimate asks that API, both through tools.py. A real restaurant
    agent works this way, and it is what lets Vocto see the tool calls and check the database.
The nodes, prompts and every other function are exactly as upstream.
"""

from typing import TypedDict

from loguru import logger
from pipecat.flows import FlowManager, NodeConfig  # VOCTO: was `from pipecat_flows import ...`

import tools  # VOCTO: the agent's HTTP tool client


# Type definitions
class PizzaOrderResult(TypedDict):
    size: str
    type: str
    price: float


class SushiOrderResult(TypedDict):
    count: int
    type: str
    price: float


class DeliveryEstimateResult(TypedDict):
    time: str


# Pre-action handlers
async def check_kitchen_status(action: dict, flow_manager: FlowManager) -> None:
    """Check if kitchen is open and log status."""
    logger.info("Checking kitchen status")


# Functions for Initial Node
async def choose_pizza(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """
    User wants to order pizza. Let's get that order started.
    """
    return None, create_pizza_node()


async def choose_sushi(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """
    User wants to order sushi. Let's get that order started.
    """
    return None, create_sushi_node()


# Functions for Pizza Node
async def select_pizza_order(
    flow_manager: FlowManager, size: str, pizza_type: str
) -> tuple[PizzaOrderResult, NodeConfig]:
    """
    Record the pizza order details.

    Args:
        size (str): Size of the pizza. Must be one of "small", "medium", or "large".
        pizza_type (str): Type of pizza. Must be one of "pepperoni", "cheese", "supreme", or "vegetarian".
    """
    # Simple pricing
    base_price = {"small": 10.00, "medium": 15.00, "large": 20.00}
    price = base_price[size]

    result = PizzaOrderResult(size=size, type=pizza_type, price=price)

    # Store order details in flow state
    flow_manager.state["order"] = {
        "type": "pizza",
        "size": size,
        "pizza_type": pizza_type,
        "price": price,
    }

    return result, create_confirmation_node()


# Functions for Sushi Node
async def select_sushi_order(
    flow_manager: FlowManager, count: int, roll_type: str
) -> tuple[SushiOrderResult, NodeConfig]:
    """
    Record the sushi order details.

    Args:
        count (int): Number of sushi rolls to order. Must be between 1 and 10.
        roll_type (str): Type of sushi roll. Must be one of "california", "spicy tuna", "rainbow", or "dragon".
    """
    # Simple pricing: $8 per roll
    price = count * 8.00

    result = SushiOrderResult(count=count, type=roll_type, price=price)

    # Store order details in flow state
    flow_manager.state["order"] = {
        "type": "sushi",
        "count": count,
        "roll_type": roll_type,
        "price": price,
    }

    return result, create_confirmation_node()


# Functions for Confirmation Node
async def complete_order(flow_manager: FlowManager) -> tuple[dict, NodeConfig | None]:
    """
    User confirms the order is correct.
    """
    # VOCTO: send the order to the restaurant (upstream: `return None, create_end_node()`).
    # If the order system fails, the flow stays here and the LLM gets the error to tell the caller.
    result = await tools.call("place_order", order=flow_manager.state.get("order", {}))
    if not result["ok"]:
        error = result["body"].get("error") if isinstance(result["body"], dict) else None
        return {"status": "failed", "error": error or f"order system error {result['status']}"}, None
    flow_manager.state["order_id"] = result["body"].get("order_id")
    return {"status": "placed", **result["body"]}, create_end_node()


# VOCTO: upstream defines this inside run_bot as a "global" function and computes the time itself;
# here it asks the restaurant's API.
async def get_delivery_estimate(
    flow_manager: FlowManager,
) -> tuple[DeliveryEstimateResult, None]:
    """Provide delivery estimate information."""
    result = await tools.call("delivery_estimate", now=tools.call_params.get().get("now", ""))
    if not result["ok"]:
        return {"status": "failed", "error": "the delivery estimate is not available right now"}, None
    return DeliveryEstimateResult(time=str(result["body"].get("time", ""))), None


async def revise_order(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """
    User wants to make changes to their order.
    """
    return None, create_initial_node()


# Node creation functions
def create_initial_node() -> NodeConfig:
    """Create the initial node for food type selection."""
    return NodeConfig(
        name="initial",
        role_message="You are an order-taking assistant. You must ALWAYS use the available functions to progress the conversation. This is a phone conversation and your responses will be converted to audio. Keep the conversation friendly, casual, and polite. Avoid outputting special characters and emojis.",
        task_messages=[
            {
                "role": "developer",
                "content": "For this step, ask the user if they want pizza or sushi, and wait for them to use a function to choose. Start off by greeting them. Be friendly and casual; you're taking an order for food over the phone.",
            }
        ],
        pre_actions=[
            {
                "type": "function",
                "handler": check_kitchen_status,
            },
        ],
        functions=[choose_pizza, choose_sushi],
    )


def create_pizza_node() -> NodeConfig:
    """Create the pizza ordering node."""
    return NodeConfig(
        name="choose_pizza",
        task_messages=[
            {
                "role": "developer",
                "content": """You are handling a pizza order. Use the available functions:
- Use select_pizza_order when the user specifies both size AND type

Pricing:
- Small: $10
- Medium: $15
- Large: $20

Remember to be friendly and casual.""",
            }
        ],
        functions=[select_pizza_order],
    )


def create_sushi_node() -> NodeConfig:
    """Create the sushi ordering node."""
    return NodeConfig(
        name="choose_sushi",
        task_messages=[
            {
                "role": "developer",
                "content": """You are handling a sushi order. Use the available functions:
- Use select_sushi_order when the user specifies both count AND type

Pricing:
- $8 per roll

Remember to be friendly and casual.""",
            }
        ],
        functions=[select_sushi_order],
    )


def create_confirmation_node() -> NodeConfig:
    """Create the order confirmation node."""
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": """Read back the complete order details to the user and ask if they want anything else or if they want to make changes. Use the available functions:
- Use complete_order when the user confirms that the order is correct and no changes are needed
- Use revise_order if they want to change something

Be friendly and clear when reading back the order details.""",
            }
        ],
        functions=[complete_order, revise_order],
    )


def create_end_node() -> NodeConfig:
    """Create the final node."""
    return NodeConfig(
        name="end",
        task_messages=[
            {
                "role": "developer",
                "content": "Thank the user for their order and end the conversation politely and concisely.",
            }
        ],
        post_actions=[{"type": "end_conversation"}],
    )
