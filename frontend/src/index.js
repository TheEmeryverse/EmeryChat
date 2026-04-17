import * as React from 'react'
import { createRoot } from 'react-dom/client'
import {
    // ErrorComponent,
    RouterProvider,
    createRouter,
    createRootRoute,
    createRoute
  } from '@tanstack/react-router'
import { Home } from './home'
// import { Login } from './login'
// import { SignUp } from './signup'
import './styles.css';

const rootRoute = createRootRoute()

const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: Home,
})

// const loginRoute = createRoute({
//   getParentRoute: () => rootRoute,
//   path: 'login',
//   component: Login,
// })

// const signupRoute = createRoute({
//   getParentRoute: () => rootRoute,
//   path: 'signup',
//   component: SignUp,
// })

const routeTree = rootRoute.addChildren([
  indexRoute,
  // loginRoute, signupRoute
])

// Create a new router instance
const router = createRouter({ routeTree })

// Render your React component instead
const root = createRoot(document.getElementById('root'))
root.render(<RouterProvider router={router} />)
