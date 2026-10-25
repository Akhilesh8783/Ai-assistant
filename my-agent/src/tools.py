import logging
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError
from importlib import import_module

DuckDuckGoSearchRun = import_module("langchain_community.tools").DuckDuckGoSearchRun

@function_tool()
async def search_web(context: RunContext, query: str):
        """
        Use this tool to search the web for information related to given query.
    
        """
        try:
            result= DuckDuckGoSearchRun().run(tool_input=query)
            logging.info(f"Web searchresult for query '{query}': {result}")
            return result
        except Exception as e :
            logging.error(f"Error occured while searching the web query '{query}': {e}")
            raise
        
