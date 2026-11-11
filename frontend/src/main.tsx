import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./index.css";
import Layout from "./Layout";
import Dashboard from "./pages/Dashboard";
import OpenItems from "./pages/OpenItems";
import Pair from "./pages/Pair";
import RelatedParties from "./pages/RelatedParties";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter basename="/intelligence">
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="pair/:a/:b" element={<Pair />} />
          <Route path="related-parties" element={<RelatedParties />} />
          <Route path="open-items" element={<OpenItems />} />
          <Route path="*" element={<Dashboard />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);
